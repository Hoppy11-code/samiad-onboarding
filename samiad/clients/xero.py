"""Xero Accounting API. OAuth 2 refresh tokens rotate on every use, so the
newest one is saved to the store straight away."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from ..rules import Chain, ChainDoc, money
from .http import ApiError, request

TOKEN_URL = "https://identity.xero.com/connect/token"
API = "https://api.xero.com/api.xro/2.0"
SCOPES = ("openid profile email offline_access accounting.invoices accounting.payments "
          "accounting.contacts accounting.settings.read")
DEAL_ID = re.compile(r"^(\d{9,13})(?:-(?:CN)?\d+)?$")


def _xero_date(value) -> date | None:
    """Xero returns '/Date(1712345678000+0000)/' or ISO strings."""
    if not value:
        return None
    m = re.search(r"/Date\((\d+)", str(value))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
    return date.fromisoformat(str(value)[:10])


class Xero:
    def __init__(self, client_id: str, client_secret: str, store, tenant_id: str = "",
                 initial_refresh_token: str = ""):
        self.client_id, self.client_secret, self.store = client_id, client_secret, store
        self.http = httpx.Client(timeout=60)
        self._access = None
        if initial_refresh_token and not store.get("xero_refresh_token"):
            store.set("xero_refresh_token", initial_refresh_token)
        self.tenant_id = tenant_id or store.get("xero_tenant_id", "")

    # ---- auth ------------------------------------------------------------
    def _token(self) -> str:
        if self._access:
            return self._access
        rt = self.store.get("xero_refresh_token")
        if not rt:
            raise RuntimeError("No Xero refresh token. Run scripts/authorise_xero.py once.")
        resp = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": rt},
                              auth=(self.client_id, self.client_secret))
        if resp.status_code >= 400:
            raise ApiError("Xero auth", resp)
        tok = resp.json()
        self.store.set("xero_refresh_token", tok["refresh_token"])  # rotate immediately
        self._access = tok["access_token"]
        if not self.tenant_id:
            conns = self.http.get("https://api.xero.com/connections",
                                  headers={"Authorization": f"Bearer {self._access}"}).json()
            org = next(c for c in conns if c.get("tenantType") == "ORGANISATION")
            self.tenant_id = org["tenantId"]
            self.store.set("xero_tenant_id", self.tenant_id)
        return self._access

    def count_call(self) -> None:
        """Xero's free tier allows 1,000 calls a day; keep a tally for the daily check."""
        key = f"xero_calls:{date.today().isoformat()}"
        self.store.set(key, (self.store.get(key) or 0) + 1)

    def _req(self, method: str, path: str, idem: str | None = None, **kw):
        self.count_call()
        headers = {"Authorization": f"Bearer {self._token()}", "Xero-tenant-id": self.tenant_id,
                   "Accept": "application/json"}
        if idem:
            headers["Idempotency-Key"] = idem[:128]
        return request(self.http, "Xero", method, API + path, headers=headers, **kw)

    # ---- reading the chain for a deal -------------------------------------
    def chain(self, deal_id: str) -> tuple[Chain, dict]:
        """Every invoice / credit note numbered with this deal ID.

        Returns the Chain plus raw Xero records keyed by number.
        """
        raw: dict[str, dict] = {}
        where = f'InvoiceNumber.StartsWith("{deal_id}") AND Type=="ACCREC"'
        # page=1 makes Xero include line items in list results
        for inv in self._req("GET", "/Invoices", params={"where": where, "page": 1}).json().get("Invoices", []):
            raw[inv["InvoiceNumber"]] = inv
        cn_where = f'CreditNoteNumber.StartsWith("{deal_id}") AND Type=="ACCRECCREDIT"'
        for cn in self._req("GET", "/CreditNotes", params={"where": cn_where, "page": 1}).json().get("CreditNotes", []):
            raw[cn["CreditNoteNumber"]] = cn
        # how much of each invoice was settled by OUR credit notes (vs prepayments/overpayments)
        ours: dict[str, Decimal] = {}
        for num, rec in raw.items():
            if "CreditNoteID" in rec and DEAL_ID.match(num):
                for a in rec.get("Allocations", []) or []:
                    iid = (a.get("Invoice") or {}).get("InvoiceID")
                    if iid:
                        ours[iid] = ours.get(iid, money(0)) + money(a.get("Amount"))
        docs = []
        for num, rec in raw.items():
            m = DEAL_ID.match(num)
            if not m or m.group(1) != deal_id:
                continue  # e.g. old Zapier "id - date N" invoices: not part of the managed chain
            is_cn = "CreditNoteID" in rec
            our_credit = ours.get(rec.get("InvoiceID"), money(0))
            other_credit = money(rec.get("AmountCredited")) - our_credit  # prepayments, overpayments
            docs.append(ChainDoc(
                number=num, total=money(rec.get("Total")),
                paid=(money(rec.get("AmountPaid")) + max(other_credit, money(0))) if not is_cn else money(0),
                credited=our_credit if not is_cn else money(0),
                status=rec.get("Status", ""), is_credit_note=is_cn,
                xero_id=rec.get("CreditNoteID") if is_cn else rec.get("InvoiceID"),
            ))
        return Chain(deal_id, docs), raw

    def any_invoice_for_deal(self, deal_id: str) -> list[str]:
        """Numbers of ANY sales invoice mentioning this deal ID (used for cutover)."""
        where = f'InvoiceNumber.Contains("{deal_id}") AND Type=="ACCREC" AND Status!="VOIDED" AND Status!="DELETED"'
        invs = self._req("GET", "/Invoices", params={"where": where}).json().get("Invoices", [])
        # Contains() would also match this ID inside a longer one; keep exact-ID hits only
        pat = re.compile(rf"(?<!\d){deal_id}(?!\d)")
        return [i["InvoiceNumber"] for i in invs if pat.search(i["InvoiceNumber"])]

    def invoice(self, invoice_id: str) -> dict:
        return self._req("GET", f"/Invoices/{invoice_id}").json()["Invoices"][0]

    def invoices_modified_since(self, since: datetime) -> list[dict]:
        out, page = [], 1
        hdr = {"If-Modified-Since": since.strftime("%Y-%m-%dT%H:%M:%S")}
        while True:
            self.count_call()
            resp = request(self.http, "Xero", "GET", API + "/Invoices",
                           headers={"Authorization": f"Bearer {self._token()}",
                                    "Xero-tenant-id": self.tenant_id, "Accept": "application/json", **hdr},
                           params={"page": page, "where": 'Type=="ACCREC"', "summaryOnly": "true"})
            batch = resp.json().get("Invoices", [])
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def all_credit_notes(self) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self._req("GET", "/CreditNotes", params={
                "page": page, "where": 'Type=="ACCRECCREDIT"'}).json().get("CreditNotes", [])
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    # ---- contacts ---------------------------------------------------------
    def find_or_create_contact(self, name: str, existing_id: str = "", account_number: str = "") -> str:
        """Xero contact for an agent company / parent, keyed by HubSpot record id.

        account_number (e.g. 'HS-C-123') is stored on the Xero contact, so two
        customers with the same name never share a contact. Names must be
        unique in Xero, so a clash gets ' (2)' etc.
        """
        if existing_id:
            return existing_id
        if account_number:
            found = self._req("GET", "/Contacts", params={
                "where": f'AccountNumber=="{account_number}"'}).json().get("Contacts", [])
            if found:
                return found[0]["ContactID"]
        safe = name.replace('"', "")
        candidate, n = safe, 1
        while True:
            clash = self._req("GET", "/Contacts", params={"where": f'Name=="{candidate}"'}).json().get("Contacts", [])
            if not clash:
                break
            if not account_number and clash:
                return clash[0]["ContactID"]
            n += 1
            candidate = f"{safe} ({n})"
        body = {"Name": candidate}
        if account_number:
            body["AccountNumber"] = account_number
        resp = self._req("PUT", "/Contacts", idem=f"contact-{account_number or candidate}",
                         json={"Contacts": [body]}).json()
        return resp["Contacts"][0]["ContactID"]

    def set_contact_email(self, contact_id: str, email: str) -> None:
        self._req("POST", f"/Contacts/{contact_id}",
                  json={"Contacts": [{"ContactID": contact_id, "EmailAddress": email}]})

    # ---- writing ---------------------------------------------------------
    @staticmethod
    def _line_items(lines, codes: dict, ministay: bool) -> list[dict]:
        items = []
        for l in lines:
            if l.kind == "course":
                code = codes["course_ministay"] if ministay else codes["course"]
            else:
                code = codes.get(l.kind, codes["course"])
            items.append({"Description": l.description, "Quantity": 1,
                          "UnitAmount": str(l.amount), "AccountCode": code, "TaxType": "NONE"})
        return items

    def create_invoice(self, *, contact_id, number, reference, lines, codes, ministay,
                       due: date, idem: str) -> dict:
        body = {"Invoices": [{
            "Type": "ACCREC", "Contact": {"ContactID": contact_id}, "InvoiceNumber": number,
            "Reference": reference, "Date": date.today().isoformat(), "DueDate": due.isoformat(),
            "LineAmountTypes": "NoTax", "CurrencyCode": "GBP", "Status": "AUTHORISED",
            "LineItems": self._line_items(lines, codes, ministay),
        }]}
        return self._req("PUT", "/Invoices", idem=idem, json=body).json()["Invoices"][0]

    def replace_invoice_lines(self, invoice_id: str, lines, codes, ministay, idem: str) -> dict:
        body = {"Invoices": [{"InvoiceID": invoice_id,
                              "LineItems": self._line_items(lines, codes, ministay)}]}
        return self._req("POST", f"/Invoices/{invoice_id}", idem=idem, json=body).json()["Invoices"][0]

    def void_invoice(self, invoice_id: str) -> None:
        self._req("POST", f"/Invoices/{invoice_id}",
                  json={"Invoices": [{"InvoiceID": invoice_id, "Status": "VOIDED"}]})

    def create_credit_note(self, *, contact_id, number, reference, description, amount: Decimal,
                           code: str, idem: str) -> dict:
        body = {"CreditNotes": [{
            "Type": "ACCRECCREDIT", "Contact": {"ContactID": contact_id},
            "CreditNoteNumber": number, "Reference": reference, "Date": date.today().isoformat(),
            "LineAmountTypes": "NoTax", "CurrencyCode": "GBP", "Status": "AUTHORISED",
            "LineItems": [{"Description": description, "Quantity": 1, "UnitAmount": str(amount),
                           "AccountCode": code, "TaxType": "NONE"}],
        }]}
        return self._req("PUT", "/CreditNotes", idem=idem, json=body).json()["CreditNotes"][0]

    def allocate_credit(self, credit_note_id: str, invoice_id: str, amount: Decimal) -> None:
        body = {"Allocations": [{"Invoice": {"InvoiceID": invoice_id}, "Amount": str(amount),
                                 "Date": date.today().isoformat()}]}
        self._req("PUT", f"/CreditNotes/{credit_note_id}/Allocations", json=body)

    def email_invoice(self, invoice_id: str) -> None:
        """Xero emails the invoice (with the Pay-with-Flywire link) to the contact's email."""
        self._req("POST", f"/Invoices/{invoice_id}/Email", json={})

    def invoice_pdf(self, invoice_id: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token()}", "Xero-tenant-id": self.tenant_id,
                   "Accept": "application/pdf"}
        return request(self.http, "Xero", "GET", f"{API}/Invoices/{invoice_id}", headers=headers).content


def invoice_line_signature(rec: dict) -> list[tuple[str, str]]:
    """(description, amount) pairs from a raw Xero invoice, for comparing lines."""
    return sorted((" ".join((li.get("Description") or "").split()),
                   str(money(li.get("LineAmount", li.get("UnitAmount")))))
                  for li in rec.get("LineItems", []))
