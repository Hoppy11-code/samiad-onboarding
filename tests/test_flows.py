"""End-to-end flow tests with in-memory stand-ins for HubSpot, Xero, Graph and Teams."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from samiad import config, flows
from samiad.rules import PIPELINE_B2B_NEW, PIPELINE_B2C_SALES
from samiad.store import Store

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
WON = "closedwon"


class FakeHubSpot:
    def __init__(self):
        self.deals, self.contacts, self.companies = {}, {}, {}
        self.deal_contacts_map, self.deal_company_map = {}, {}
        self.notes = []

    def closed_won_stages(self):
        return {WON}

    def get(self, obj, oid, props):
        store = {"deals": self.deals, "companies": self.companies, "contacts": self.contacts}[obj]
        return {"id": oid, "properties": dict(store[oid])}

    def update(self, obj, oid, props):
        store = {"deals": self.deals, "companies": self.companies, "contacts": self.contacts}[obj]
        store[oid].update(props)

    def deal_contacts(self, deal_id):
        return [{"id": c, "properties": dict(self.contacts[c])} for c in self.deal_contacts_map[deal_id]]

    def deal_primary_company(self, deal_id):
        cid = self.deal_company_map.get(deal_id)
        return {"id": cid, "properties": dict(self.companies[cid])} if cid else None

    def owner(self, oid):
        return {"name": "Lauren Hints", "email": "lauren@samiad.com"}

    def add_deal_note(self, deal_id, text):
        self.notes.append((deal_id, text))


class FakeXero:
    def __init__(self):
        self.invoices, self.credit_notes, self.emailed, self.contact_emails = {}, {}, [], {}
        self.legacy = []
        self.contact_lookups = []

    # reads
    def chain(self, deal_id):
        from samiad.clients.xero import Xero as Real  # reuse the real parsing via a shim
        from samiad.rules import Chain, ChainDoc
        docs, raw = [], {}
        for num, inv in self.invoices.items():
            if num == deal_id or num.startswith(deal_id + "-"):
                raw[num] = inv
                docs.append(ChainDoc(num, inv["Total"], inv["AmountPaid"], inv.get("AmountCredited", D(0)),
                                     inv["Status"], False, inv["InvoiceID"]))
        for num, cn in self.credit_notes.items():
            if num.startswith(deal_id + "-CN"):
                raw[num] = cn
                docs.append(ChainDoc(num, cn["Total"], status=cn["Status"], is_credit_note=True,
                                     xero_id=cn["CreditNoteID"]))
        return Chain(deal_id, docs), raw

    def any_invoice_for_deal(self, deal_id):
        return [n for n in self.legacy if deal_id in n] + [n for n in self.invoices if deal_id in n]

    # writes
    def find_or_create_contact(self, name, existing="", account_number="", emails=(), domain=""):
        self.contact_lookups.append({"name": name, "emails": list(emails), "domain": domain})
        return existing or f"contact-{name}"

    def set_contact_email(self, cid, email):
        self.contact_emails[cid] = email

    def _lines(self, lines):
        return [{"Description": l.description, "LineAmount": str(l.amount)} for l in lines]

    def create_invoice(self, *, contact_id, number, reference, lines, codes, ministay, due, idem):
        total = sum((l.amount for l in lines), D(0))
        inv = {"InvoiceID": f"id-{number}", "InvoiceNumber": number, "Total": total, "AmountPaid": D(0),
               "AmountDue": total, "Status": "AUTHORISED", "Contact": {"ContactID": contact_id},
               "LineItems": self._lines(lines)}
        self.invoices[number] = inv
        return inv

    def replace_invoice_lines(self, invoice_id, lines, codes, ministay, idem):
        inv = next(i for i in self.invoices.values() if i["InvoiceID"] == invoice_id)
        assert inv["AmountPaid"] == 0, "must never edit a paid invoice"
        inv["LineItems"] = self._lines(lines)
        inv["Total"] = inv["AmountDue"] = sum((l.amount for l in lines), D(0))
        return inv

    def void_invoice(self, invoice_id):
        inv = next(i for i in self.invoices.values() if i["InvoiceID"] == invoice_id)
        inv["Status"] = "VOIDED"

    def create_credit_note(self, *, contact_id, number, reference, description, amount, code, idem):
        cn = {"CreditNoteID": f"id-{number}", "CreditNoteNumber": number, "Total": amount, "Status": "AUTHORISED"}
        self.credit_notes[number] = cn
        return cn

    def allocate_credit(self, cn_id, invoice_id, amount):
        inv = next(i for i in self.invoices.values() if i["InvoiceID"] == invoice_id)
        inv["AmountCredited"] = inv.get("AmountCredited", D(0)) + amount
        inv["AmountDue"] -= amount

    def email_invoice(self, invoice_id):
        self.emailed.append(invoice_id)

    def pay(self, number, amount):
        inv = self.invoices[number]
        inv["AmountPaid"] += D(amount)
        inv["AmountDue"] -= D(amount)


class FakeGraph:
    def __init__(self):
        self.files, self.mail = {}, []

    def ensure_folder(self, path):
        pass

    def upload(self, folder, name, data):
        self.files[f"{folder}/{name}"] = data
        return ""

    def download(self, folder, name):
        return self.files.get(f"{folder}/{name}")

    def folder_url(self, folder):
        return "https://sharepoint/x"

    def send_mail(self, sender, to, subject, html, attachments=()):
        self.mail.append({"to": to, "subject": subject, "files": [a[0] for a in attachments]})


class FakeTeams:
    def __init__(self):
        self.posts = []
        self.buttons = []

    def post(self, title, lines, link=None, links=()):
        self.posts.append((title, lines))
        self.buttons.append([b for b in [*links, link] if b])


def student(cid, net="1350", gross="1900", ins="12"):
    return {"contact_type": "Student", "firstname": "Kid", "lastname": cid, "course": "General English",
            "specialism": "Multi-Activity", "campus": "The Leys School", "arrival_dats": "2027-07-22",
            "departure_date": "2027-07-29", "date_of_birth": "9/21/2012", "nationality": "Italian",
            "passport_number": "YA123", "net_price__": net, "gross_price__": gross, "insurance_fee__": ins,
            "airport_transfer_fee": "0", "airport_transfers": "Return Transfers", "ensuite_supplement": "0",
            "unaccompanied_minor_fee": "0", "pre_post_online_course__": "0", "visiting_year": "2027",
            "total_net_fee": str(D(net) + D(ins)), "gross_fee__total_": str(D(gross) + D(ins))}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(flows.docs, "to_pdf", lambda b: b"%PDF fake")
    # only_deals / test_email_to come from a local .env during trials; tests must not depend on them
    s = replace(config.load(), shadow_mode=False, data_dir=tmp_path, sharepoint_folder="General/Applications/2027",
                only_deals=frozenset(), test_email_to="")
    hs, xero, graph, teams = FakeHubSpot(), FakeXero(), FakeGraph(), FakeTeams()
    svc = flows.Service(s, hs, xero, graph, teams, Store(tmp_path), now=NOW)
    entered = str(int((NOW - timedelta(hours=2)).timestamp() * 1000))
    hs.deals["900000000001"] = {"dealname": "Rossi family", "pipeline": PIPELINE_B2C_SALES, "dealstage": WON,
                                "hubspot_owner_id": "1", "hs_v2_date_entered_current_stage": entered,
                                "createdate": "2026-10-01T09:00:00Z", "visiting_year": "2027"}
    hs.contacts["s1"] = student("s1")
    hs.contacts["p1"] = {"contact_type": "Parent / Guardian", "firstname": "Maria", "lastname": "Rossi"}
    hs.deal_contacts_map["900000000001"] = ["s1", "p1"]
    return svc, hs, xero, graph, teams


def test_new_b2c_booking_full_journey(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    inv = xero.invoices["900000000001"]
    assert inv["Total"] == D("1362")                      # B2C = net
    assert xero.contact_emails["contact-Maria Rossi"] == "lauren@samiad.com"
    assert xero.emailed == ["id-900000000001"]           # Xero emails invoice to deal owner
    assert graph.mail[0]["to"] == ["lauren@samiad.com"]
    assert any(f.endswith("Confirmation & Invoice v1 - Rossi family.pdf") for f in graph.files)
    assert any("Visa Letter v1" in f and f.endswith(".docx") for f in graph.files)
    assert hs.deals["900000000001"]["samiad_managed"] == "true"

    # re-running changes nothing (no duplicate invoice)
    svc.process("900000000001")
    assert len(xero.invoices) == 1 and len(graph.mail) == 1

    # price change before payment: edit in place
    hs.contacts["s1"] = student("s1", net="1500")
    svc.process("900000000001")
    assert xero.invoices["900000000001"]["Total"] == D("1512") and len(xero.invoices) == 1
    assert graph.mail[-1]["subject"].startswith("REVISED")

    # deposit paid: receipt, no visa pack yet (under 50%)
    xero.pay("900000000001", "700")
    svc.process("900000000001")
    assert hs.deals["900000000001"]["payment_status"] == "Part paid"
    assert graph.mail[-1]["subject"].startswith("Receipt")

    # passes 50%: visa pack
    xero.pay("900000000001", "100")
    svc.process("900000000001")
    assert graph.mail[-1]["subject"].startswith("Visa letters")
    assert hs.deals["900000000001"]["visa_pack_sent_date"]
    sent = len(graph.mail)
    svc.process("900000000001")
    assert len(graph.mail) == sent                       # never sent twice

    # price rise after part payment: top-up, base untouched
    hs.contacts["s1"] = student("s1", net="1600")
    svc.process("900000000001")
    assert xero.invoices["900000000001-1"]["Total"] == D("100")
    assert xero.invoices["900000000001"]["Total"] == D("1512")

    # price drop: credit note allocated to what's owed
    hs.contacts["s1"] = student("s1", net="1450")
    svc.process("900000000001")
    assert xero.credit_notes["900000000001-CN1"]["Total"] == D("150")


def test_invoicing_tells_teams_with_xero_check_and_folder_link(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    (title, lines), buttons = teams.posts[-1], teams.buttons[-1]
    assert title == "Booking invoiced: Rossi family"
    assert any("Invoice 900000000001 created for £1362" in l for l in lines)
    assert any(l.startswith("✅ Checked in Xero") and "£1362" in l for l in lines)
    assert ("Open documents folder", "https://sharepoint/x") in buttons
    assert any(t == "Open deal" for t, _ in buttons)

    # a later change is announced as an update, still checked against Xero
    hs.contacts["s1"] = student("s1", net="1500")
    svc.process("900000000001")
    assert teams.posts[-1][0] == "Booking updated: Rossi family"
    assert any(l.startswith("✅ Checked in Xero") and "£1512" in l for l in teams.posts[-1][1])

    # nothing changed: no post
    n = len(teams.posts)
    svc.process("900000000001")
    assert len(teams.posts) == n


def test_teams_flags_when_xero_total_does_not_match(env):
    svc, hs, xero, graph, teams = env
    real_create = xero.create_invoice

    def short_create(**kw):  # Xero ends up holding less than we sent
        inv = real_create(**kw)
        inv["Total"] -= D(10)
        return inv
    xero.create_invoice = short_create
    svc.process("900000000001")
    assert any(l.startswith("⚠️ Xero shows £1352") for l in teams.posts[-1][1])


def test_shadow_mode_announces_as_trial_without_folder_link(env):
    svc, hs, xero, graph, teams = env
    svc.s = replace(svc.s, shadow_mode=True)
    svc.process("900000000001")
    (title, lines), buttons = teams.posts[-1], teams.buttons[-1]
    assert title.startswith("TRIAL - Booking invoiced")
    assert any("nothing was actually created" in l for l in lines)
    assert not any(t == "Open documents folder" for t, _ in buttons)
    assert not xero.invoices


def test_xero_contact_lookup_gets_parent_email_and_agent_details(env):
    svc, hs, xero, graph, teams = env
    fresh_deal = dict(hs.deals["900000000001"])
    hs.contacts["p1"]["email"] = "maria@example.it"
    svc.process("900000000001")
    assert xero.contact_lookups[-1] == {"name": "Maria Rossi", "emails": ["maria@example.it"], "domain": ""}

    hs.deals["900000000002"] = {**fresh_deal, "pipeline": PIPELINE_B2B_NEW}
    hs.companies["c1"] = {"name": "Agency X", "billing_basis": "Net", "domain": "agencyx.com"}
    hs.deal_company_map["900000000002"] = "c1"
    hs.contacts["a1"] = {"contact_type": "Agent", "firstname": "Ana", "lastname": "X", "email": "ana@agencyx.com"}
    hs.contacts["s2"] = student("s2")
    hs.deal_contacts_map["900000000002"] = ["s2", "a1"]
    svc.process("900000000002")
    assert xero.contact_lookups[-1] == {"name": "Agency X", "emails": ["ana@agencyx.com"], "domain": "agencyx.com"}


def test_b2b_without_billing_basis_is_blocked_and_alerts_once(env):
    svc, hs, xero, graph, teams = env
    hs.deals["900000000001"]["pipeline"] = PIPELINE_B2B_NEW
    hs.companies["c1"] = {"name": "Agency X"}
    hs.deal_company_map["900000000001"] = "c1"
    svc.process("900000000001")
    svc.process("900000000001")
    assert not xero.invoices
    assert len([p for p in teams.posts if p[0] == "Booking can't be invoiced yet"]) == 1
    assert hs.deals["900000000001"]["samiad_status"].startswith("Blocked")


def test_b2b_gross_agent_invoiced_gross_to_agent(env):
    svc, hs, xero, graph, teams = env
    hs.deals["900000000001"]["pipeline"] = PIPELINE_B2B_NEW
    hs.companies["c1"] = {"name": "Agency X", "billing_basis": "Gross"}
    hs.deal_company_map["900000000001"] = "c1"
    svc.process("900000000001")
    assert xero.invoices["900000000001"]["Total"] == D("1912")
    assert hs.companies["c1"]["xero_contact_id"] == "contact-Agency X"


def test_one_hour_safeguard_defers(env):
    svc, hs, xero, graph, teams = env
    hs.deals["900000000001"]["hs_v2_date_entered_current_stage"] = str(int((NOW - timedelta(minutes=20)).timestamp() * 1000))
    assert svc.process("900000000001") == "defer"
    assert not xero.invoices


def test_cutover_leaves_old_zapier_invoices_alone(env):
    svc, hs, xero, graph, teams = env
    xero.legacy = ["900000000001 - 14-08-2026N"]
    svc.process("900000000001")
    assert not xero.invoices
    assert hs.deals["900000000001"]["samiad_status"].startswith("Not managed")


def test_big_change_waits_for_approval(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    xero.pay("900000000001", "500")
    hs.contacts["s1"] = student("s1", net="3000")
    svc.process("900000000001")
    assert "900000000001-1" not in xero.invoices
    assert hs.deals["900000000001"]["samiad_status"].startswith("Waiting for approval")
    hs.deals["900000000001"]["approve_change"] = "true"
    svc.process("900000000001")
    assert xero.invoices["900000000001-1"]["Total"] == D("1650")
    assert hs.deals["900000000001"]["approve_change"] == "false"


def test_stale_approval_tick_is_cleared_not_reused(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    hs.deals["900000000001"]["approve_change"] = "true"       # ticked with nothing pending
    svc.process("900000000001")
    assert hs.deals["900000000001"]["approve_change"] == "false"
    xero.pay("900000000001", "500")
    hs.contacts["s1"] = student("s1", net="3000")
    svc.process("900000000001")
    assert "900000000001-1" not in xero.invoices                # still waits for a fresh tick


def test_previous_season_deal_is_never_touched(env):
    svc, hs, xero, graph, teams = env
    hs.deals["900000000001"]["visiting_year"] = "2026"
    svc.process("900000000001")
    assert not xero.invoices


def test_old_student_record_on_this_years_deal_blocks(env):
    svc, hs, xero, graph, teams = env
    hs.contacts["s1"]["visiting_year"] = "2026"
    svc.process("900000000001")
    assert not xero.invoices
    assert "Visiting year" in hs.deals["900000000001"]["samiad_status"]


def test_blank_deal_year_is_left_alone(env):
    svc, hs, xero, graph, teams = env
    hs.deals["900000000001"].pop("visiting_year")
    svc.process("900000000001")
    assert not xero.invoices and not teams.posts
    assert "samiad_status" not in hs.deals["900000000001"]


def test_blank_student_year_on_2027_deal_blocks(env):
    svc, hs, xero, graph, teams = env
    hs.contacts["s1"].pop("visiting_year")
    svc.process("900000000001")
    assert not xero.invoices
    assert "Visiting year on Kid s1" in hs.deals["900000000001"]["samiad_status"]
    assert teams.posts[-1][0] == "Booking can't be invoiced yet"


def test_voided_base_on_managed_deal_is_not_recreated(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    xero.invoices["900000000001"]["Status"] = "VOIDED"
    svc.process("900000000001", xero_changed=True)
    assert len(xero.invoices) == 1
    assert hs.deals["900000000001"]["samiad_status"].startswith("Blocked: base invoice")


def test_own_invoice_is_not_mistaken_for_legacy(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    hs.deals["900000000001"].pop("samiad_managed")             # e.g. the HubSpot write had failed
    svc.process("900000000001")
    assert not hs.deals["900000000001"].get("samiad_status", "").startswith("Not managed")
    assert len(xero.invoices) == 1


def test_unchanged_deal_skips_xero(env):
    svc, hs, xero, graph, teams = env
    svc.process("900000000001")
    calls = []
    xero.chain = lambda d, _orig=xero.chain: calls.append(d) or _orig(d)
    svc.process("900000000001", xero_changed=False)
    assert calls == []


def test_shadow_state_does_not_leak_into_live(env):
    svc, hs, xero, graph, teams = env
    svc.s = replace(svc.s, shadow_mode=True)
    svc.process("900000000001")
    svc.s = replace(svc.s, shadow_mode=False)
    svc.process("900000000001")
    assert "900000000001" in xero.invoices and hs.deals["900000000001"]["samiad_managed"] == "true"


def test_failed_deal_is_retried(env, monkeypatch):
    svc, hs, xero, graph, teams = env
    hs.students_modified_since = lambda since: []
    hs.closed_won_deals_modified_since = lambda since: [{"id": "900000000001"}]
    xero.invoices_modified_since = lambda since: []
    monkeypatch.setattr(svc, "process", lambda d, xero_changed=True: (_ for _ in ()).throw(RuntimeError("Xero down")))
    svc.run()
    assert svc.sget("deferred") == ["900000000001"]


def test_shadow_mode_writes_nothing(env, tmp_path):
    svc, hs, xero, graph, teams = env
    svc.s = replace(svc.s, shadow_mode=True)
    svc.process("900000000001")
    assert not xero.invoices and not graph.mail and not graph.files
    assert "samiad_managed" not in hs.deals["900000000001"]
    assert any((tmp_path / "shadow").rglob("*.docx"))   # documents still produced locally to compare


def test_ministay_waits_for_address_then_sends(env):
    from samiad.rules import PIPELINE_MINISTAY
    svc, hs, xero, graph, teams = env
    hs.deals["900000000001"]["pipeline"] = PIPELINE_MINISTAY
    hs.companies["c1"] = {"name": "Agency X"}                    # no basis set: Ministay is net anyway
    hs.deal_company_map["900000000001"] = "c1"
    svc.process("900000000001")
    assert xero.invoices["900000000001"]["Total"] == D("1362")   # net
    xero.pay("900000000001", "700")
    svc.process("900000000001", xero_changed=True)
    assert not any(m["subject"].startswith("Visa letters") for m in graph.mail)
    assert any(t == "Ministay visa letters need the address" for t, _ in teams.posts)
    # staff type the address into the Word file in Teams
    from samiad import documents as docs
    key = next(k for k in graph.files if "Visa Letter" in k)
    import io
    import docx as _docx
    from docx.oxml.ns import qn
    d = _docx.Document(io.BytesIO(graph.files[key]))
    for t in d.element.body.iter(qn("w:t")):
        if t.text and docs.MINISTAY_ADDRESS_PLACEHOLDER in t.text:
            t.text = t.text.replace(docs.MINISTAY_ADDRESS_PLACEHOLDER, "12 Host Street")
    buf = io.BytesIO()
    d.save(buf)
    graph.files[key] = buf.getvalue()
    svc.process("900000000001", xero_changed=True)
    assert any(m["subject"].startswith("Visa letters") for m in graph.mail)


def test_test_mode_sends_everything_to_one_inbox_and_limits_deals(env):
    svc, hs, xero, graph, teams = env
    svc.s = replace(svc.s, test_email_to="alex@samiad.com", only_deals=frozenset({"900000000001"}))
    assert svc.process("999999999999") is None
    svc.process("900000000001")
    assert graph.mail[0]["to"] == ["alex@samiad.com"]
    assert set(xero.contact_emails.values()) == {"alex@samiad.com"}
