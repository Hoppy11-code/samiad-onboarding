"""HubSpot CRM API (EU portal). Auth: a Service Key or private-app token."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .http import request

BASE = "https://api.hubapi.com"

STUDENT_PROPS = [
    "firstname", "lastname", "contact_type", "email", "date_of_birth", "nationality",
    "passport_number", "course", "specialism", "campus", "arrival_dats", "departure_date",
    "net_price__", "gross_price__", "insurance_fee__", "airport_transfer_fee",
    "airport_transfers", "ensuite_supplement", "unaccompanied_minor_fee",
    "pre_post_online_course__", "total_net_fee", "gross_fee__total_", "lastmodifieddate",
]
DEAL_PROPS = [
    "dealname", "createdate", "pipeline", "dealstage", "hubspot_owner_id", "closedate", "hs_lastmodifieddate",
    "hs_v2_date_entered_current_stage", "net_total_calculated", "gross_total_calculated",
    "campus", "arrival_date", "departure_date", "start_date_of_course", "number_of_nights",
    "invoice_amount", "xero_invoice_number", "payment_status", "total_paid", "remaining_balance",
    "invoiced_date", "visa_pack_sent_date", "resync_to_xero", "approve_change",
    "samiad_managed", "samiad_status", "samiad_notes",
]
COMPANY_PROPS = ["name", "billing_basis", "xero_contact_id", "hubspot_owner_id"]
PARENT_PROPS = ["firstname", "lastname", "email", "contact_type", "xero_contact_id"]


class HubSpot:
    def __init__(self, token: str):
        self.c = httpx.Client(base_url=BASE, timeout=60,
                              headers={"Authorization": f"Bearer {token}"})
        self._owners: dict[str, dict] = {}
        self._closed_won: set[str] | None = None

    def _req(self, method, path, **kw):
        return request(self.c, "HubSpot", method, path, **kw)

    # ---- search -----------------------------------------------------------
    def search(self, obj: str, filters: list[dict], props: list[str], limit: int = 100,
               sorts: list[dict] | None = None) -> list[dict]:
        out, after = [], None
        while True:
            body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": limit}
            if sorts:
                body["sorts"] = sorts
            if after:
                body["after"] = after
            data = self._req("POST", f"/crm/v3/objects/{obj}/search", json=body).json()
            out.extend(data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                return out

    def students_modified_since(self, since: datetime) -> list[dict]:
        ms = str(int(since.timestamp() * 1000))
        return self.search("contacts", [
            {"propertyName": "contact_type", "operator": "EQ", "value": "Student"},
            {"propertyName": "lastmodifieddate", "operator": "GTE", "value": ms},
        ], ["firstname", "lastname", "lastmodifieddate"])

    def closed_won_deals_modified_since(self, since: datetime) -> list[dict]:
        ms = str(int(since.timestamp() * 1000))
        return self.search("deals", [
            {"propertyName": "dealstage", "operator": "IN", "values": sorted(self.closed_won_stages())},
            {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": ms},
        ], DEAL_PROPS)

    def managed_open_deals(self) -> list[dict]:
        """Every deal the service manages that isn't fully paid yet."""
        return self.search("deals", [
            {"propertyName": "samiad_managed", "operator": "EQ", "value": "true"},
            {"propertyName": "payment_status", "operator": "NEQ", "value": "Paid"},
        ], DEAL_PROPS)

    def all_managed_deals(self) -> list[dict]:
        return self.search("deals", [
            {"propertyName": "samiad_managed", "operator": "EQ", "value": "true"},
        ], DEAL_PROPS)

    # ---- records ----------------------------------------------------------
    def get(self, obj: str, oid: str, props: list[str]) -> dict:
        return self._req("GET", f"/crm/v3/objects/{obj}/{oid}",
                         params={"properties": ",".join(props)}).json()

    def batch_read(self, obj: str, ids: list[str], props: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            data = self._req("POST", f"/crm/v3/objects/{obj}/batch/read",
                             json={"inputs": [{"id": x} for x in chunk], "properties": props}).json()
            out.extend(data.get("results", []))
        return out

    def update(self, obj: str, oid: str, props: dict) -> None:
        clean = {k: ("" if v is None else str(v)) for k, v in props.items()}
        self._req("PATCH", f"/crm/v3/objects/{obj}/{oid}", json={"properties": clean})

    # ---- associations -----------------------------------------------------
    def associations(self, from_obj: str, oid: str, to_obj: str) -> list[dict]:
        """[{id, labels:[...]}] using the v4 associations API."""
        out, after = [], None
        while True:
            params = {"limit": 500}
            if after:
                params["after"] = after
            data = self._req("GET", f"/crm/v4/objects/{from_obj}/{oid}/associations/{to_obj}",
                             params=params).json()
            for r in data.get("results", []):
                labels = [t.get("label") or "" for t in r.get("associationTypes", [])]
                out.append({"id": str(r["toObjectId"]), "labels": labels})
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                return out

    def deal_contacts(self, deal_id: str) -> list[dict]:
        ids = [a["id"] for a in self.associations("deals", deal_id, "contacts")]
        if not ids:
            return []
        return self.batch_read("contacts", ids, sorted(set(STUDENT_PROPS + PARENT_PROPS)))

    def deal_primary_company(self, deal_id: str) -> dict | None:
        assoc = self.associations("deals", deal_id, "companies")
        if not assoc:
            return None
        primary = [a for a in assoc if "Primary" in a["labels"]] or assoc
        return self.get("companies", primary[0]["id"], COMPANY_PROPS)

    def contact_deals(self, contact_id: str) -> list[str]:
        return [a["id"] for a in self.associations("contacts", contact_id, "deals")]

    # ---- owners / pipelines ----------------------------------------------
    def owner(self, owner_id: str) -> dict:
        if owner_id not in self._owners:
            o = self._req("GET", f"/crm/v3/owners/{owner_id}").json()
            name = f"{o.get('firstName') or ''} {o.get('lastName') or ''}".strip()
            self._owners[owner_id] = {"name": name or o.get("email", ""), "email": o.get("email", "")}
        return self._owners[owner_id]

    def closed_won_stages(self) -> set[str]:
        """Stage ids labelled 'Closed Won' in any deal pipeline (looked up, not hard-coded)."""
        if self._closed_won is None:
            data = self._req("GET", "/crm/v3/pipelines/deals").json()
            self._closed_won = {
                st["id"] for p in data.get("results", []) for st in p.get("stages", [])
                if (st.get("label") or "").strip().lower() == "closed won"
            }
        return self._closed_won

    # ---- notes (audit trail on the deal) --------------------------------
    def add_deal_note(self, deal_id: str, text: str) -> None:
        now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        self._req("POST", "/crm/v3/objects/notes", json={
            "properties": {"hs_note_body": text, "hs_timestamp": now_ms},
            "associations": [{"to": {"id": deal_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]}],
        })
