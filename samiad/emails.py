"""Emails to salespeople. Short and plain; the attachments do the work.

The 50% email text is a placeholder until Alex sends the real wording:
replace templates/emails/fifty_percent.html (Jinja placeholders allowed).
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

EMAILS = Path(__file__).resolve().parent.parent / "templates" / "emails"
env = Environment(loader=FileSystemLoader(EMAILS), autoescape=select_autoescape(["html"]))


def render(name: str, **ctx) -> str:
    return env.get_template(name).render(**ctx)
