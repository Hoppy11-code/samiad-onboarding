# Samiad booking automation

Replaces Zapier for booking finance and student onboarding. Every weekday hour
(09:00–17:00 London) it:

1. **Syncs students → deals.** Student contacts (Contact Type = Student) are the
   source of truth. The deal's totals are rebuilt from its students.
2. **Invoices new bookings** once a deal has been Closed Won for an hour:
   creates the Xero invoice (numbered with the deal ID), files the
   *Confirmation & Invoice* (Word + PDF) and visa letters (Word) in
   Teams → HQ → General → Applications → 2027, emails the salesperson the PDF,
   and has Xero email the invoice + Flywire link to the salesperson.
3. **Follows changes.** Unpaid invoice → edited in place. Anything paid → top-up
   invoice (`{deal}-1`) or credit note (`{deal}-CN1`). Never a second invoice.
4. **Tracks payments** from Xero (Flywire settles invoices directly) → updates
   the deal, files a receipt and emails it to the salesperson.
5. **At 50% paid** emails the salesperson the visa letters (PDF, made from the
   current Word files, so edits in Teams carry through) and the pre-arrival pack.
6. **Daily check at 09:00** posts only problems to Teams (HQ → General).

The full design is in the build spec (Claude doc "Samiad Finance & Onboarding
Agent — Build Spec").

## Safety

* **Shadow mode** (`SHADOW_MODE=true`, the default) does everything except
  write: no Xero, HubSpot, SharePoint or email changes. Documents it would have
  sent are saved under `data/shadow/` and every intended action is in the audit
  log. Run like this alongside Zapier before switching over.
* **Cutover:** any deal that already has an invoice in Xero from before
  switch-on is marked *Not managed* and never touched.
* Price changes over `APPROVAL_THRESHOLD` (default £1,000), or credits that
  would need a refund, wait for someone to tick **Approve change** on the deal.
* B2B bookings are blocked until the agent company has **Billing basis** set.
  B2C is always net. Nobody picks net or gross by hand.
* If a student's fee fields don't add up to their total, the booking is
  blocked rather than invoiced wrong.

## Setup (once)

You need Python 3.12 and LibreOffice on your computer for local testing.
Claude Code can do every step below with you.

1. `cp .env.example .env` and fill it in (keys from your password manager).
   **Never paste keys into a chat.**
2. **HubSpot key:** Settings → Integrations → Service Keys (beta) → create a key
   with scopes `crm.objects.contacts.read/write`, `crm.objects.deals.read/write`,
   `crm.objects.companies.read/write`, `crm.schemas.contacts.write`,
   `crm.schemas.companies.write`, `crm.schemas.deals.write`, `crm.objects.owners.read`.
   If Service Keys aren't available, create a private app with the same scopes.
3. **Create the HubSpot properties:**
   `python scripts/setup_hubspot_properties.py` (preview) then `--apply`.
   Then set **Billing basis** on agents with 2027 bookings.
4. **Connect Xero:** `python scripts/authorise_xero.py` → approve in the
   browser → copy `XERO_TENANT_ID` and `XERO_REFRESH_TOKEN` into `.env`.
   *Test against the Xero Demo Company first*, then re-run for Samiad Limited.
5. **Lock down Microsoft 365** (PowerShell, as admin):
   * SharePoint: grant the app *write* on the HQ site only (Sites.Selected):
     `New-MgSitePermission -SiteId <HQ site id> -Roles write -GrantedToIdentities @{application=@{id="<client id>";displayName="Samiad Onboarding"}}`
   * Mail: let it send as bookings@ only (RBAC for Applications):
     `New-ServicePrincipal -AppId <client id> -ObjectId <enterprise app object id> -DisplayName "Samiad Onboarding"`
     `New-ManagementScope -Name "Samiad bookings mailbox" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'bookings@samiad.com'"`
     `New-ManagementRoleAssignment -App <client id> -Role "Application Mail.Send" -CustomResourceScope "Samiad bookings mailbox"`
6. **Check everything connects:** `python -m samiad.main --check`
7. **Try one deal in shadow mode:** `python -m samiad.main --deal <deal id>`
   and look in `data/shadow/<deal id>/` and the log.

## Deploy on Railway

1. railway.app → New Project → Deploy from GitHub → `samiad-onboarding`.
2. Variables: paste everything from `.env` (keep `SHADOW_MODE=true`).
3. Add a **Volume** mounted at `/data` (stores the Xero token, run times, audit log).
4. The schedule comes from `railway.json` (hourly on weekdays; the code skips
   outside 09:00–17:00 London).
5. Watch the logs for two weeks in shadow mode, compare with Zapier, then set
   `SHADOW_MODE=false` and switch Zapier off.

> The Xero refresh token changes every time it's used. Once Railway is running,
> don't reuse the same token locally; run `authorise_xero.py` again for local
> testing (ideally against the Demo Company).

## Running by hand

```
python -m samiad.main --check        # test connections, change nothing
python -m samiad.main --deal 123     # process one deal now
python -m samiad.main --force        # full run now, ignoring the time window
python -m samiad.main --report       # send the daily check now
python -m pytest                     # rules and flow tests
python scripts/prepare_templates.py  # rebuild templates after editing templates/source/
python scripts/render_samples.py     # sample documents into out/ to eyeball
```

## Templates

Originals from Samiad are in `templates/source/` (never edited by code).
`scripts/prepare_templates.py` turns them into the tagged versions in
`templates/` and applies the agreed fixes (campus instead of Box Hill School,
per-student rows for groups, receipt layout, deal owner instead of Sam Allen).
To change wording: edit the source file in Word, then re-run the script.

Still to come: Ministay visa letter (`templates/visa_ministay_net.docx` /
`_gross.docx`; until then the standard letter is used) and the real 50% email
wording (`templates/emails/fifty_percent.html`).

## Known limits / to check at first test

* Xero emails invoices to the contact's email, so the service sets the Xero
  contact's email to the deal owner before sending. Xero reminders for that
  contact also go to the salesperson.
* Xero's free Starter tier allows 1,000 API calls a day. The hourly run only
  looks at what changed, so normal days use a small fraction of that.
* `net_total_calculated` / `gross_total_calculated` on the deal are written by
  the service. If HubSpot rejects that (e.g. they're calculated properties),
  invoicing carries on and Teams gets one alert.
* Unmatched bank receipts are not in the daily check yet (needs the Xero
  bank-transactions permission).
