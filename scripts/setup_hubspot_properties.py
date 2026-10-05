"""Create the HubSpot properties the service needs (safe to re-run; existing ones are skipped).

    python scripts/setup_hubspot_properties.py          # show what would be created
    python scripts/setup_hubspot_properties.py --apply  # create them

Needs a HubSpot key with the crm.schemas.*.write scopes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samiad import config  # noqa: E402
from samiad.clients.hubspot import HubSpot  # noqa: E402

GROUP = {"companies": "companyinformation", "deals": "dealinformation", "contacts": "contactinformation"}

PROPS = [
    ("companies", "billing_basis", "Billing basis", "enumeration", "select",
     [("Net", "Net"), ("Gross", "Gross")], "How this agent is invoiced. Required before a B2B booking can be invoiced."),
    ("companies", "xero_contact_id", "Xero contact ID", "string", "text", None, "Set by the booking automation."),
    ("contacts", "xero_contact_id", "Xero contact ID", "string", "text", None, "Set by the booking automation (B2C parents)."),
    ("deals", "invoice_amount", "Invoice amount", "number", "number", None, "What the Xero invoice chain should total. Set by the automation."),
    ("deals", "xero_invoice_number", "Xero invoice number", "string", "text", None, "Set by the automation."),
    ("deals", "payment_status", "Payment status", "enumeration", "select",
     [("Unpaid", "Unpaid"), ("Part paid", "Part paid"), ("Paid", "Paid")], "Set by the automation from Xero."),
    ("deals", "approve_change", "Approve change", "bool", "booleancheckbox",
     [("Yes", "true"), ("No", "false")], "Tick to approve a large or refund-causing price change."),
    ("deals", "visa_pack_sent_date", "Visa pack sent date", "date", "date", None, "Set when the 50% email goes out."),
    ("deals", "samiad_managed", "Managed by automation", "bool", "booleancheckbox",
     [("Yes", "true"), ("No", "false")], "Set by the automation once it creates the invoice."),
    ("deals", "samiad_status", "Automation status", "string", "text", None, "OK, Blocked: ..., Waiting for approval: ..., Not managed ..."),
]


def main(apply: bool):
    s = config.load()
    hs = HubSpot(s.hubspot_token)
    for obj, name, label, typ, field, options, desc in PROPS:
        try:
            hs._req("GET", f"/crm/v3/properties/{obj}/{name}")
            print(f"exists   {obj}.{name}")
            continue
        except Exception:
            pass
        body = {"name": name, "label": label, "type": typ, "fieldType": field,
                "groupName": GROUP[obj], "description": desc}
        if options:
            body["options"] = [{"label": l, "value": v, "displayOrder": i} for i, (l, v) in enumerate(options)]
        if apply:
            hs._req("POST", f"/crm/v3/properties/{obj}", json=body)
            print(f"created  {obj}.{name}")
        else:
            print(f"would create {obj}.{name}")


if __name__ == "__main__":
    main("--apply" in sys.argv)
