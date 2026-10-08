"""Entry point. Railway runs `python -m samiad.main` every hour.

    python -m samiad.main            # normal run (respects weekday/hours)
    python -m samiad.main --force    # run now regardless of time
    python -m samiad.main --deal 123 # process one deal (testing)
    python -m samiad.main --report   # send the daily report now
    python -m samiad.main --check    # check every connection, change nothing
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config
from .clients.hubspot import HubSpot
from .clients.microsoft import Graph, Teams
from .clients.xero import Xero
from .flows import Service
from .report import ai_summary, daily_report
from .store import Store

log = logging.getLogger("samiad")


def build(settings=None) -> Service:
    s = settings or config.load()
    store = Store(s.data_dir)
    return Service(
        s,
        HubSpot(s.hubspot_token),
        Xero(s.xero_client_id, s.xero_client_secret, store, s.xero_tenant_id, s.xero_initial_refresh_token),
        Graph(s.ms_tenant_id, s.ms_client_id, s.ms_client_secret, s.sharepoint_site),
        Teams(s.teams_webhook_url),
        store,
    )


def in_hours(s, now_utc: datetime) -> bool:
    local = now_utc.astimezone(ZoneInfo(s.timezone))
    if s.run_weekdays_only and local.weekday() >= 5:
        return False
    return s.run_hours[0] <= local.hour <= s.run_hours[1]


def send_report(svc: Service) -> None:
    issues = daily_report(svc)
    if not issues:
        log.info("daily report: all clear")
        return
    summary = ai_summary(svc, issues)
    lines = ([summary, "—"] if summary else []) + [f"• {i}" for i in issues[:40]]
    if len(issues) > 40:
        lines.append(f"…and {len(issues) - 40} more")
    svc.teams.post(f"Daily booking check: {len(issues)} item{'s' if len(issues) != 1 else ''}", lines)


def check(svc: Service) -> int:
    """Prove each credential works. Read-only."""
    ok = True
    for name, fn in [
        ("HubSpot", lambda: f"{len(svc.hs.closed_won_stages())} 'Closed Won' stages found"),
        ("Xero", lambda: f"tenant {svc.xero._token() and svc.xero.tenant_id}"),
        ("SharePoint", lambda: f"site {svc.graph.site_id()[:40]}…"),
        ("Teams webhook", lambda: "configured" if svc.s.teams_webhook_url else "MISSING"),
    ]:
        try:
            print(f"OK   {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"FAIL {name}: {e}")
    print(f"Shadow mode: {'ON' if svc.s.shadow_mode else 'OFF'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO; the Teams webhook URL carries its secret (sig=...)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--deal")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)

    svc = build()
    if a.check:
        return check(svc)
    if a.deal:
        svc.process(a.deal)
        return 0
    if a.report:
        send_report(svc)
        return 0

    now = datetime.now(timezone.utc)
    if not a.force and not in_hours(svc.s, now):
        log.info("outside working hours, nothing to do")
        return 0
    svc.run()
    local = now.astimezone(ZoneInfo(svc.s.timezone))
    today = local.date().isoformat()
    if local.hour >= svc.s.run_hours[0] and svc.store.get("report_day") != today:
        send_report(svc)
        svc.store.set("report_day", today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
