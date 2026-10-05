"""One-time: connect the service to Samiad's Xero and save the refresh token.

Run on your own computer (it opens a browser and listens on localhost:8080):

    python scripts/authorise_xero.py

You'll log in to Xero and approve access. The script then prints a refresh
token: paste it into Railway as XERO_REFRESH_TOKEN (and your local .env).
After the first run the service keeps the token fresh by itself, as long as
it runs at least once every 60 days.
"""
import http.server
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from samiad import config  # noqa: E402
from samiad.clients.xero import SCOPES, TOKEN_URL  # noqa: E402

REDIRECT = "http://localhost:8080/callback"


def main():
    s = config.load()
    state = secrets.token_urlsafe(16)
    url = "https://login.xero.com/identity/connect/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": s.xero_client_id, "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state})
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Xero connected. You can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    print("Opening Xero in your browser...")
    webbrowser.open(url)
    print("If it didn't open, paste this into your browser:\n", url)
    http.server.HTTPServer(("localhost", 8080), Handler).handle_request()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"Authorisation failed: {result}")
    tok = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": result["code"],
                                      "redirect_uri": REDIRECT},
                     auth=(s.xero_client_id, s.xero_client_secret)).json()
    conns = httpx.get("https://api.xero.com/connections",
                      headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    print("\nConnected organisations:")
    for c in conns:
        print(f"  {c['tenantName']}  ->  XERO_TENANT_ID={c['tenantId']}")
    print(f"\nXERO_REFRESH_TOKEN={tok['refresh_token']}\n")
    print("Put both values into Railway's Variables (and your .env). Keep them private.")


if __name__ == "__main__":
    main()
