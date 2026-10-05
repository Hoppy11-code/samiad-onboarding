"""Settings, read from environment variables (Railway "Variables", or a local .env)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0]  # allow "KEY=value   # comment"
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Missing setting {name} (see .env.example)")
    return v or ""


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be true or false, got {v!r}")  # never guess on shadow mode


DEFAULT_ACCOUNT_CODES = {
    "course": "203",           # Summer School Sales
    "course_ministay": "205",  # Sales - Mini stay
    "airport_transfer_fee": "203",
    "insurance_fee__": "203",
    "pre_post_online_course__": "203",
    "ensuite_supplement": "203",
    "unaccompanied_minor_fee": "203",
    "topup": "203",
}


@dataclass(frozen=True)
class Settings:
    # behaviour
    shadow_mode: bool
    approval_threshold: Decimal
    data_dir: Path
    timezone: str
    run_hours: tuple[int, int]      # local hours, inclusive
    run_weekdays_only: bool
    season_label: str               # e.g. "2027", used in folder names
    season_start: date              # deals created before this are never touched
    payment_terms_days: int
    account_codes: dict
    # HubSpot
    hubspot_token: str
    hubspot_portal_id: str
    # Xero
    xero_client_id: str
    xero_client_secret: str
    xero_tenant_id: str
    xero_initial_refresh_token: str
    # Microsoft 365
    ms_tenant_id: str
    ms_client_id: str
    ms_client_secret: str
    sharepoint_site: str            # e.g. samiad.sharepoint.com:/sites/HQ
    sharepoint_folder: str          # e.g. General/Applications/2027
    mail_from: str                  # bookings@samiad.com
    teams_webhook_url: str
    # optional AI summary
    anthropic_api_key: str
    claude_model: str


def load() -> Settings:
    data_dir = Path(_env("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    codes = dict(DEFAULT_ACCOUNT_CODES)
    if os.environ.get("ACCOUNT_CODES"):
        codes.update(json.loads(os.environ["ACCOUNT_CODES"]))
    start, end = (int(x) for x in _env("RUN_HOURS", "9-17").split("-"))
    return Settings(
        shadow_mode=_bool("SHADOW_MODE", True),
        approval_threshold=Decimal(_env("APPROVAL_THRESHOLD", "1000")),
        data_dir=data_dir,
        timezone=_env("TIMEZONE", "Europe/London"),
        run_hours=(start, end),
        run_weekdays_only=_bool("RUN_WEEKDAYS_ONLY", True),
        season_label=_env("SEASON", "2027"),
        season_start=date.fromisoformat(_env("SEASON_START", "2026-09-01")),
        payment_terms_days=int(_env("PAYMENT_TERMS_DAYS", "14")),
        account_codes=codes,
        hubspot_token=_env("HUBSPOT_TOKEN"),
        hubspot_portal_id=_env("HUBSPOT_PORTAL_ID", "146042835"),
        xero_client_id=_env("XERO_CLIENT_ID"),
        xero_client_secret=_env("XERO_CLIENT_SECRET"),
        xero_tenant_id=_env("XERO_TENANT_ID"),
        xero_initial_refresh_token=_env("XERO_REFRESH_TOKEN"),
        ms_tenant_id=_env("MS_TENANT_ID"),
        ms_client_id=_env("MS_CLIENT_ID"),
        ms_client_secret=_env("MS_CLIENT_SECRET"),
        sharepoint_site=_env("SHAREPOINT_SITE"),
        sharepoint_folder=_env("SHAREPOINT_FOLDER", "General/Applications/2027"),
        mail_from=_env("MAIL_FROM", "bookings@samiad.com"),
        teams_webhook_url=_env("TEAMS_WEBHOOK_URL"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        claude_model=_env("CLAUDE_MODEL", "claude-sonnet-5-5"),
    )
