"""The 09:00 daily check, posted to Teams. Issues only; silence means all clear."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from .rules import money

log = logging.getLogger("samiad")
NUM = re.compile(r"^(\d{9,13})(?:-(CN)?\d+)?$")


def daily_report(svc) -> list[str]:
    hs, xero, s = svc.hs, svc.xero, svc.s
    issues: list[str] = []
    managed = {d["id"]: d["properties"] for d in hs.all_managed_deals()}

    # Xero side: group every sales invoice / credit note by deal ID
    by_deal: dict[str, dict] = defaultdict(lambda: {"bases": 0, "invoiced": money(0), "odd": []})
    for inv in xero.invoices_modified_since(datetime(2000, 1, 1, tzinfo=timezone.utc)):
        if inv.get("Status") in ("VOIDED", "DELETED", "DRAFT"):
            continue
        num = inv.get("InvoiceNumber", "")
        m = NUM.match(num)
        if m:
            entry = by_deal[m.group(1)]
            entry["invoiced"] += money(inv.get("Total"))
            if num == m.group(1):
                entry["bases"] += 1
        else:
            for deal_id in managed:
                if deal_id in num:
                    by_deal[deal_id]["odd"].append(num)
    for cn in xero.all_credit_notes():
        if cn.get("Status") in ("VOIDED", "DELETED", "DRAFT"):
            continue
        m = NUM.match(cn.get("CreditNoteNumber", ""))
        if m:
            by_deal[m.group(1)]["invoiced"] -= money(cn.get("Total"))

    for deal_id, p in managed.items():
        name = p.get("dealname") or deal_id
        x = by_deal.get(deal_id)
        if not x or x["bases"] == 0:
            issues.append(f"{name}: managed but no invoice {deal_id} found in Xero")
            continue
        if x["bases"] > 1:
            issues.append(f"{name}: {x['bases']} invoices numbered {deal_id} in Xero (duplicate)")
        target = money(p.get("invoice_amount"))
        if target and x["invoiced"] != target:
            issues.append(f"{name}: Xero totals £{x['invoiced']} but the booking should be £{target}")
        for odd in x["odd"]:
            issues.append(f"{name}: invoice '{odd}' was made outside the system")

    # HubSpot side: stuck bookings
    stuck = hs.search("deals", [
        {"propertyName": "dealstage", "operator": "IN", "values": sorted(hs.closed_won_stages())},
        {"propertyName": "samiad_status", "operator": "HAS_PROPERTY"},
    ], ["dealname", "samiad_status"])
    for d in stuck:
        st = d["properties"].get("samiad_status") or ""
        if st.startswith(("Blocked", "Waiting")):
            issues.append(f"{d['properties'].get('dealname')}: {st}")

    cutoff = str(int((svc.now - timedelta(hours=24)).timestamp() * 1000))
    uninvoiced = hs.search("deals", [
        {"propertyName": "dealstage", "operator": "IN", "values": sorted(hs.closed_won_stages())},
        {"propertyName": "samiad_managed", "operator": "NOT_HAS_PROPERTY"},
        {"propertyName": "samiad_status", "operator": "NOT_HAS_PROPERTY"},
        {"propertyName": "hs_v2_date_entered_current_stage", "operator": "LT", "value": cutoff},
        {"propertyName": "createdate", "operator": "GTE", "value": str(int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000))},
    ], ["dealname"])
    for d in uninvoiced[:25]:
        issues.append(f"{d['properties'].get('dealname')}: Closed Won over 24 hours ago but not invoiced")

    if s.shadow_mode:
        issues.insert(0, "Shadow mode is ON: nothing was written to Xero, HubSpot, Teams files or email.")
    return issues


def ai_summary(svc, issues: list[str]) -> str:
    """Optional three-line summary from Claude. Falls back to nothing."""
    if not svc.s.anthropic_api_key or not issues:
        return ""
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": svc.s.anthropic_api_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json={
            "model": svc.s.claude_model, "max_tokens": 300,
            "messages": [{"role": "user", "content":
                "These are today's issues from a summer school's booking-to-invoice automation. "
                "In at most three short plain-English lines, say what needs a person today and "
                "what can wait. No preamble.\n\n" + "\n".join(issues[:80])}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception:
        log.exception("AI summary failed")
        return ""
