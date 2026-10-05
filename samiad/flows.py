"""What happens to each booking on every run. See the build spec, "End-to-end flows".

Every write (Xero, HubSpot, SharePoint, email) goes through `self.write()`, so
shadow mode can run the whole thing for real while changing nothing.
"""
from __future__ import annotations

import logging
import os
import re
import traceback
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import documents as docs
from . import emails
from .clients.hubspot import DEAL_PROPS, HubSpot
from .clients.microsoft import Graph, Teams
from .clients.xero import Xero, invoice_line_signature
from .config import Settings
from .rules import (Action, Basis, BlockedError, Chain, Line, Student, ZERO, PIPELINE_MINISTAY,
                    B2B_PIPELINES, billing_basis, booking_lines, decide, half_paid, money,
                    payment_status, total_of)
from .store import Store

log = logging.getLogger("samiad")
ONE_HOUR = timedelta(hours=1)


def _safe(name: str) -> str:
    """SharePoint won't take  \" * : < > ? / \\ |  in names."""
    return re.sub(r'[\\/:*?"<>|#%]+', "-", name).strip(" .") or "Unnamed"


def _hs_time(value) -> datetime | None:
    if not value:
        return None
    s = str(value)
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Service:
    def __init__(self, settings: Settings, hubspot: HubSpot, xero: Xero, graph: Graph,
                 teams: Teams, store: Store, now: datetime | None = None):
        self.s, self.hs, self.xero, self.graph, self.teams, self.store = (
            settings, hubspot, xero, graph, teams, store)
        self.now = now or datetime.now(timezone.utc)
        self.summary: list[str] = []

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def write(self, deal_id: str, what: str, fn, *args, **kwargs):
        """Do a write, or in shadow mode just record what would have happened."""
        self.store.log(deal_id, what, {"args": [str(a)[:200] for a in args]}, shadow=self.s.shadow_mode)
        if self.s.shadow_mode:
            log.info("[shadow] %s %s", deal_id, what)
            return None
        return fn(*args, **kwargs)

    def once(self, key: str, signature: str) -> bool:
        """True the first time a (key, signature) pair is seen: stops hourly repeats."""
        if self.store.get(f"once:{key}") == signature:
            return False
        self.store.set(f"once:{key}", signature)
        return True

    def alert(self, deal_id: str, title: str, lines: list[str], once_sig: str | None = None):
        if once_sig is not None and not self.once(f"alert:{deal_id}:{title}", once_sig):
            return
        link = ("Open deal", self.deal_url(deal_id)) if deal_id else None
        try:
            self.teams.post(title, lines, link)
        except Exception:  # never let a Teams hiccup stop the run
            log.exception("Teams post failed")

    def deal_url(self, deal_id: str) -> str:
        return f"https://app-eu1.hubspot.com/contacts/{self.s.hubspot_portal_id}/record/0-3/{deal_id}"

    def set_status(self, deal_id: str, status: str):
        key = f"status:{deal_id}"
        if self.store.get(key) == status:
            return
        self.store.set(key, status)
        self.write(deal_id, f"status: {status}", self.hs.update, "deals", deal_id,
                   {"samiad_status": status[:250]})

    # ------------------------------------------------------------------
    # the hourly run
    # ------------------------------------------------------------------
    def run(self) -> None:
        cursor = _hs_time(self.store.get("cursor")) or (self.now - timedelta(hours=2))
        quiet_until = self.now - timedelta(minutes=10)  # leave half-finished edits alone
        candidates: set[str] = set(self.store.get("deferred", []))

        # 0. student sync: changed students -> their deals
        for c in self.hs.students_modified_since(cursor - timedelta(minutes=15)):
            modified = _hs_time(c["properties"].get("lastmodifieddate"))
            if modified and modified > quiet_until:
                continue  # picked up next run
            candidates.update(self.hs.contact_deals(c["id"]))

        # deals changed directly (stage moved to Closed Won, resync/approve ticked)
        for d in self.hs.closed_won_deals_modified_since(cursor - timedelta(minutes=15)):
            candidates.add(d["id"])

        # payments: Xero invoices changed since last run -> their deals
        for inv in self.xero.invoices_modified_since(cursor - timedelta(minutes=15)):
            m = re.match(r"^(\d{9,13})(?:-\d+)?$", inv.get("InvoiceNumber", ""))
            if m:
                candidates.add(m.group(1))

        deferred: set[str] = set()
        for deal_id in sorted(candidates):
            try:
                if self.process(deal_id) == "defer":
                    deferred.add(deal_id)
            except Exception as e:
                log.exception("deal %s failed", deal_id)
                self.store.log(deal_id, "error", traceback.format_exc()[-2000:], self.s.shadow_mode)
                self.alert(deal_id, "Booking automation hit an error",
                           [f"Deal {deal_id}: {str(e)[:300]}", "It will retry next hour."],
                           once_sig=str(e)[:100])
        self.store.set("deferred", sorted(deferred))
        self.store.set("cursor", quiet_until.isoformat())

    # ------------------------------------------------------------------
    # one booking
    # ------------------------------------------------------------------
    def process(self, deal_id: str) -> str | None:
        deal = self.hs.get("deals", deal_id, DEAL_PROPS)
        p = deal["properties"]
        name = p.get("dealname") or deal_id
        managed = (p.get("samiad_managed") or "").lower() == "true"

        if p.get("dealstage") not in self.hs.closed_won_stages():
            if managed:
                self.alert(deal_id, "Invoiced booking is no longer Closed Won",
                           [f"{name}: the deal was moved out of Closed Won after it was invoiced. "
                            "Nothing has been changed in Xero; please check."], once_sig=p.get("dealstage"))
            return None

        # safeguard: only act once the deal has sat at Closed Won for an hour
        entered = _hs_time(p.get("hs_v2_date_entered_current_stage"))
        if not managed and entered and self.now - entered < ONE_HOUR:
            return "defer"

        # cutover: never touch bookings invoiced before switch-on
        if not managed:
            if (p.get("samiad_status") or "").startswith("Not managed"):
                return None
            existing = self.xero.any_invoice_for_deal(deal_id)
            if existing:
                self.set_status(deal_id, "Not managed - invoiced before switch-on ("
                                + ", ".join(existing[:3]) + ")")
                return None

        contacts = self.hs.deal_contacts(deal_id)
        students = [Student(c["id"], dict(c["properties"])) for c in contacts
                    if (c["properties"].get("contact_type") or "") == "Student"]
        parent = next((c for c in contacts if (c["properties"].get("contact_type") or "").startswith("Parent")), None)
        company = self.hs.deal_primary_company(deal_id)
        pipeline = p.get("pipeline") or ""
        b2b = pipeline in B2B_PIPELINES
        for st in students:
            st.props["_nights"] = docs.nights(st)

        try:
            basis = billing_basis(pipeline, (company or {}).get("properties", {}).get("billing_basis"))
            if b2b and not company:
                raise BlockedError("No agent company is attached to this deal")
            if not b2b and not parent:
                raise BlockedError("No Parent / Guardian contact is attached to this deal")
            lines = booking_lines(students, basis)
        except BlockedError as e:
            self.set_status(deal_id, f"Blocked: {e}")
            self.alert(deal_id, "Booking can't be invoiced yet", [f"{name}: {e}"], once_sig=str(e))
            return None

        owner = self.hs.owner(p.get("hubspot_owner_id")) if p.get("hubspot_owner_id") else None
        if not owner or not owner.get("email"):
            self.set_status(deal_id, "Blocked: deal has no owner")
            self.alert(deal_id, "Booking can't be invoiced yet", [f"{name}: the deal has no owner."],
                       once_sig="no-owner")
            return None

        customer = (company["properties"].get("name") if b2b else
                    f"{parent['properties'].get('firstname') or ''} {parent['properties'].get('lastname') or ''}".strip())
        ctx = Ctx(deal_id, name, p, basis, students, lines, company, parent, b2b, customer, owner,
                  pipeline == PIPELINE_MINISTAY)

        # Rule 0: the deal's totals come from its students
        net_total = sum((s.stated_total(Basis.NET) for s in students), ZERO)
        gross_total = sum((s.stated_total(Basis.GROSS) for s in students), ZERO)
        target = total_of(lines)
        updates = {}
        for prop, val in (("net_total_calculated", net_total), ("gross_total_calculated", gross_total),
                          ("invoice_amount", target)):
            if money(p.get(prop)) != val:
                updates[prop] = str(val)
        if updates:
            try:
                self.write(deal_id, f"deal totals from students {updates}", self.hs.update, "deals",
                           deal_id, updates)
            except Exception as e:  # e.g. a total that is a HubSpot calculated property
                self.alert(deal_id, "Couldn't update deal totals",
                           [f"{name}: {str(e)[:200]}", "Invoicing carried on using the students' fees."],
                           once_sig=str(e)[:80])

        chain, raw = self.xero.chain(deal_id)
        base_match = True
        if chain.base:
            want = sorted((l.description, str(l.amount)) for l in lines)
            base_match = invoice_line_signature(raw[chain.base.number]) == want
        decision = decide(chain, lines, self.s.approval_threshold, base_lines_match=base_match)

        approved = (p.get("approve_change") or "").lower() == "true"
        if decision.needs_approval and not approved:
            sig = f"{decision.action.value}:{decision.amount}"
            self.set_status(deal_id, f"Waiting for approval: {decision.action.value} £{decision.amount}")
            self.alert(deal_id, "Booking change needs approval",
                       [f"{name}: {decision.action.value} of £{decision.amount}.",
                        *[f"Why: {r}" for r in decision.reasons],
                        "To approve, tick 'Approve change' on the deal. It goes through on the next run."],
                       once_sig=sig)
            return None

        changed = decision.action is not Action.NONE
        if decision.action is Action.CREATE_BASE:
            self.create_base(ctx, target)
        elif decision.action is Action.EDIT_BASE:
            self.edit_base(ctx, chain, raw, decision)
        elif decision.action is Action.TOP_UP:
            self.top_up(ctx, chain, decision.amount)
        elif decision.action is Action.CREDIT:
            self.credit(ctx, chain, raw, decision.amount)

        if changed:
            if approved:
                self.write(deal_id, "clear approval", self.hs.update, "deals", deal_id, {"approve_change": "false"})
            if not self.s.shadow_mode:
                chain, raw = self.xero.chain(deal_id)

        self.payments(ctx, chain)
        if not (self.store.get(f"status:{deal_id}") or "").startswith("OK"):
            self.set_status(deal_id, "OK")
        return None

    # ------------------------------------------------------------------
    # Xero actions
    # ------------------------------------------------------------------
    def xero_contact(self, ctx: "Ctx") -> str:
        if ctx.b2b:
            rec, obj = ctx.company, "companies"
        else:
            rec, obj = ctx.parent, "contacts"
        existing = rec["properties"].get("xero_contact_id") or ""
        if self.s.shadow_mode:
            return existing or "shadow-contact"
        cid = self.xero.find_or_create_contact(ctx.customer, existing)
        if cid != existing:
            self.hs.update(obj, rec["id"], {"xero_contact_id": cid})
        return cid

    def send_xero_invoice_to_owner(self, ctx: "Ctx", contact_id: str, invoice_id: str):
        self.write(ctx.deal_id, f"set Xero contact email to {ctx.owner['email']}",
                   self.xero.set_contact_email, contact_id, ctx.owner["email"])
        self.write(ctx.deal_id, "Xero emails invoice to deal owner", self.xero.email_invoice, invoice_id)

    def create_base(self, ctx: "Ctx", target: Decimal):
        contact_id = self.xero_contact(ctx)
        due = date.today() + timedelta(days=self.s.payment_terms_days)
        inv = self.write(ctx.deal_id, f"create invoice {ctx.deal_id} £{target}", self.xero.create_invoice,
                         contact_id=contact_id, number=ctx.deal_id, reference=ctx.name, lines=ctx.lines,
                         codes=self.s.account_codes, ministay=ctx.ministay, due=due,
                         idem=f"create-{ctx.deal_id}")
        self.write(ctx.deal_id, "mark deal managed", self.hs.update, "deals", ctx.deal_id, {
            "samiad_managed": "true", "xero_invoice_number": ctx.deal_id,
            "invoiced_date": date.today().isoformat(), "invoice_amount": str(target)})
        self.file_and_send_confirmation(ctx, target, revised=None)
        if inv:
            self.send_xero_invoice_to_owner(ctx, contact_id, inv["InvoiceID"])
        self.note(ctx, f"Invoice {ctx.deal_id} created for £{target} ({ctx.basis.value}).")

    def edit_base(self, ctx: "Ctx", chain: Chain, raw: dict, decision):
        for num in decision.void_topups:
            self.write(ctx.deal_id, f"void unpaid top-up {num}", self.xero.void_invoice, raw[num]["InvoiceID"])
        base = raw[chain.base.number]
        self.write(ctx.deal_id, f"edit invoice {chain.base.number} to £{decision.amount}",
                   self.xero.replace_invoice_lines, base["InvoiceID"], ctx.lines, self.s.account_codes,
                   ctx.ministay, f"edit-{ctx.deal_id}-{decision.amount}")
        reason = f"invoice updated from £{chain.invoiced} to £{decision.amount}"
        self.file_and_send_confirmation(ctx, decision.amount, revised=reason)
        self.send_xero_invoice_to_owner(ctx, base["Contact"]["ContactID"], base["InvoiceID"])
        self.note(ctx, f"Invoice {chain.base.number} edited: {reason}.")

    def top_up(self, ctx: "Ctx", chain: Chain, amount: Decimal):
        number = chain.next_number(credit=False)
        contact_id = self.xero_contact(ctx)
        line = Line(f"{ctx.name} - booking change {date.today():%d %b %Y}", amount, "topup", "")
        inv = self.write(ctx.deal_id, f"create top-up {number} £{amount}", self.xero.create_invoice,
                         contact_id=contact_id, number=number, reference=ctx.name, lines=[line],
                         codes={**self.s.account_codes, "topup": self.s.account_codes["topup"]},
                         ministay=ctx.ministay, due=date.today() + timedelta(days=self.s.payment_terms_days),
                         idem=f"topup-{number}")
        reason = f"price up £{amount}, top-up invoice {number}"
        self.file_and_send_confirmation(ctx, chain.invoiced + amount, revised=reason)
        if inv:
            self.send_xero_invoice_to_owner(ctx, contact_id, inv["InvoiceID"])
        self.note(ctx, f"Top-up invoice {number} raised for £{amount}.")

    def credit(self, ctx: "Ctx", chain: Chain, raw: dict, amount: Decimal):
        number = chain.next_number(credit=True)
        contact_id = self.xero_contact(ctx)
        cn = self.write(ctx.deal_id, f"create credit note {number} £{amount}", self.xero.create_credit_note,
                        contact_id=contact_id, number=number, reference=ctx.name,
                        description=f"{ctx.name} - booking change {date.today():%d %b %Y}", amount=amount,
                        code=self.s.account_codes["course_ministay" if ctx.ministay else "course"],
                        idem=f"credit-{number}")
        remaining = amount
        for doc in [chain.base, *chain.topups]:
            if remaining <= ZERO or doc is None:
                break
            due = money(raw[doc.number].get("AmountDue"))
            take = min(due, remaining)
            if take > ZERO and cn:
                self.write(ctx.deal_id, f"allocate £{take} of {number} to {doc.number}",
                           self.xero.allocate_credit, cn["CreditNoteID"], raw[doc.number]["InvoiceID"], take)
            remaining -= take
        if remaining > ZERO:
            self.alert(ctx.deal_id, "Refund due", [
                f"{ctx.name}: credit note {number} for £{amount} leaves £{remaining} overpaid.",
                "A refund (or carrying it forward) needs a person to arrange."], once_sig=number)
        reason = f"price down £{amount}, credit note {number}"
        self.file_and_send_confirmation(ctx, chain.invoiced - amount, revised=reason)
        self.note(ctx, f"Credit note {number} raised for £{amount}.")

    def note(self, ctx: "Ctx", text: str):
        self.write(ctx.deal_id, "note on deal", self.hs.add_deal_note, ctx.deal_id, f"[Automation] {text}")

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    def folder(self, ctx: "Ctx") -> str:
        group = _safe(ctx.customer) if ctx.b2b else "B2C"
        return f"{self.s.sharepoint_folder}/{group}/{_safe(ctx.name)} - {ctx.deal_id}"

    def next_version(self, ctx: "Ctx", kind: str) -> int:
        key = f"ver:{ctx.deal_id}:{kind}"
        v = (self.store.get(key) or 0) + 1
        if not self.s.shadow_mode:
            self.store.set(key, v)
        return v

    def booking_docs(self, ctx: "Ctx", total: Decimal, received=ZERO, outstanding=ZERO) -> docs.BookingDocs:
        return docs.BookingDocs(ctx.deal_id, ctx.name, ctx.basis, ctx.students, ctx.customer,
                                ctx.owner["name"], ctx.owner["email"], total, received, outstanding)

    def save(self, ctx: "Ctx", filename: str, data: bytes):
        folder = self.folder(ctx)
        if self.s.shadow_mode:
            out = self.s.data_dir / "shadow" / _safe(ctx.deal_id)
            out.mkdir(parents=True, exist_ok=True)
            (out / filename).write_bytes(data)
        self.write(ctx.deal_id, f"upload {filename}", self._upload, folder, filename, data)

    def _upload(self, folder, filename, data):
        self.graph.ensure_folder(folder)
        return self.graph.upload(folder, filename, data)

    def file_and_send_confirmation(self, ctx: "Ctx", total: Decimal, revised: str | None):
        b = self.booking_docs(ctx, total)
        v = self.next_version(ctx, "confirmation")
        stem = f"Confirmation & Invoice v{v} - {_safe(ctx.name)}"
        word = docs.confirmation_docx(b)
        pdf = docs.to_pdf(word)
        self.save(ctx, f"{stem}.docx", word)
        self.save(ctx, f"{stem}.pdf", pdf)
        # visa letters: Word only, ready for edits or early sending from Teams
        vv = self.next_version(ctx, "visa")
        for i, st in enumerate(ctx.students):
            self.save(ctx, f"Visa Letter v{vv} - {_safe(st.name)}.docx",
                      docs.visa_docx(b, i, ministay=ctx.ministay))
        html = emails.render("confirmation.html", owner_first=ctx.owner["name"].split(" ")[0],
                             deal_name=ctx.name, deal_id=ctx.deal_id, customer_name=ctx.customer,
                             total=docs.gbp(total), basis=ctx.basis.value, revised=bool(revised),
                             reason=revised, folder_url=self.folder_url(ctx), folder_name=self.folder(ctx))
        subject = f"{'REVISED ' if revised else ''}Confirmation & invoice - {ctx.name} ({ctx.deal_id})"
        self.write(ctx.deal_id, f"email confirmation to {ctx.owner['email']}", self.graph.send_mail,
                   self.s.mail_from, [ctx.owner["email"]], subject, html, [(f"{stem}.pdf", pdf)])

    def folder_url(self, ctx: "Ctx") -> str:
        if self.s.shadow_mode:
            return ""
        return self.graph.folder_url(self.folder(ctx))

    # ------------------------------------------------------------------
    # payments, receipts and the 50% pack
    # ------------------------------------------------------------------
    def payments(self, ctx: "Ctx", chain: Chain):
        if chain.base is None:
            return
        p = ctx.props
        status = payment_status(chain).value
        paid, outstanding = chain.paid, chain.outstanding
        last_paid = money(self.store.get(f"paid:{ctx.deal_id}", p.get("total_paid")))
        updates = {}
        if money(p.get("total_paid")) != paid:
            updates["total_paid"] = str(paid)
        if money(p.get("remaining_balance")) != outstanding:
            updates["remaining_balance"] = str(outstanding)
        if (p.get("payment_status") or "") != status:
            updates["payment_status"] = status
        if updates:
            self.write(ctx.deal_id, f"payment fields {updates}", self.hs.update, "deals", ctx.deal_id, updates)

        if paid > last_paid:
            b = self.booking_docs(ctx, chain.invoiced, paid, outstanding)
            word = docs.receipt_docx(b)
            pdf = docs.to_pdf(word)
            stem = f"Receipt {date.today():%Y-%m-%d} - {_safe(ctx.name)}"
            self.save(ctx, f"{stem}.docx", word)
            self.save(ctx, f"{stem}.pdf", pdf)
            html = emails.render("receipt.html", owner_first=ctx.owner["name"].split(" ")[0],
                                 deal_name=ctx.name, deal_id=ctx.deal_id, customer_name=ctx.customer,
                                 received=docs.gbp(paid), outstanding=docs.gbp(outstanding))
            self.write(ctx.deal_id, f"email receipt to {ctx.owner['email']}", self.graph.send_mail,
                       self.s.mail_from, [ctx.owner["email"]], f"Receipt - {ctx.name} ({ctx.deal_id})",
                       html, [(f"{stem}.pdf", pdf)])
            if not self.s.shadow_mode:
                self.store.set(f"paid:{ctx.deal_id}", str(paid))

        if half_paid(chain) and not p.get("visa_pack_sent_date") and not self.store.get(f"visa:{ctx.deal_id}"):
            self.send_visa_pack(ctx, chain)

    def send_visa_pack(self, ctx: "Ctx", chain: Chain):
        folder = self.folder(ctx)
        vv = self.store.get(f"ver:{ctx.deal_id}:visa") or 1
        b = self.booking_docs(ctx, chain.invoiced)
        attachments = []
        for i, st in enumerate(ctx.students):
            fname = f"Visa Letter v{vv} - {_safe(st.name)}.docx"
            word = None if self.s.shadow_mode else self.graph.download(folder, fname)  # staff edits win
            word = word or docs.visa_docx(b, i, ministay=ctx.ministay)
            attachments.append((fname.replace(".docx", ".pdf"), docs.to_pdf(word)))
        html = emails.render("fifty_percent.html", owner_first=ctx.owner["name"].split(" ")[0],
                             deal_name=ctx.name, deal_id=ctx.deal_id, customer_name=ctx.customer,
                             received=docs.gbp(chain.paid), total=docs.gbp(chain.invoiced),
                             visa_count=len(attachments),
                             pre_arrival_guide_url=os.environ.get("PRE_ARRIVAL_GUIDE_URL", ""),
                             pre_arrival_page_url=os.environ.get("PRE_ARRIVAL_PAGE_URL", ""),
                             english_test_url=os.environ.get("ENGLISH_TEST_URL", ""))
        self.write(ctx.deal_id, f"email visa pack to {ctx.owner['email']}", self.graph.send_mail,
                   self.s.mail_from, [ctx.owner["email"]],
                   f"Visa letters & pre-arrival - {ctx.name} ({ctx.deal_id})", html, attachments)
        self.write(ctx.deal_id, "set visa_pack_sent_date", self.hs.update, "deals", ctx.deal_id,
                   {"visa_pack_sent_date": date.today().isoformat()})
        if not self.s.shadow_mode:
            self.store.set(f"visa:{ctx.deal_id}", date.today().isoformat())
        self.note(ctx, "50% paid: visa letters and pre-arrival pack emailed to the deal owner.")


class Ctx:
    """Everything known about one booking during a run."""

    def __init__(self, deal_id, name, props, basis, students, lines, company, parent, b2b,
                 customer, owner, ministay):
        self.deal_id, self.name, self.props, self.basis = deal_id, name, props, basis
        self.students, self.lines, self.company, self.parent = students, lines, company, parent
        self.b2b, self.customer, self.owner, self.ministay = b2b, customer, owner, ministay
