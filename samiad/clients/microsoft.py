"""Microsoft Graph (SharePoint files + sending mail) and the Teams webhook."""
from __future__ import annotations

import base64
import time
from urllib.parse import quote

import httpx

from .http import request

GRAPH = "https://graph.microsoft.com/v1.0"


class Graph:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, site: str):
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.site = site  # "netorgft864411.sharepoint.com:/sites/SamiadHQ"
        # file downloads (/content) answer with a redirect to a pre-signed URL; httpx drops the
        # Authorization header when the redirect goes to another host, which is what Graph expects
        self.http = httpx.Client(timeout=120, follow_redirects=True)
        self._tok, self._exp, self._site_id = None, 0.0, None

    def _token(self) -> str:
        if self._tok and time.time() < self._exp - 60:
            return self._tok
        resp = self.http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.client_secret, "scope": "https://graph.microsoft.com/.default"})
        resp.raise_for_status()
        data = resp.json()
        self._tok, self._exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._tok

    def _req(self, method, path, **kw):
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token()}"
        return request(self.http, "Graph", method, GRAPH + path, headers=headers, **kw)

    # ---- SharePoint ------------------------------------------------------
    def site_id(self) -> str:
        if not self._site_id:
            self._site_id = self._req("GET", f"/sites/{self.site}").json()["id"]
        return self._site_id

    def ensure_folder(self, path: str) -> None:
        parent = ""
        for part in [p for p in path.split("/") if p]:
            url = (f"/sites/{self.site_id()}/drive/root:/{quote(parent)}:/children" if parent
                   else f"/sites/{self.site_id()}/drive/root/children")
            try:
                self._req("POST", url, json={"name": part, "folder": {},
                                             "@microsoft.graph.conflictBehavior": "fail"})
            except Exception as e:  # already exists (409) is fine
                if "409" not in str(e) and "nameAlreadyExists" not in str(e):
                    raise
            parent = f"{parent}/{part}" if parent else part

    def upload(self, folder: str, filename: str, content: bytes) -> str:
        """Upload (replace) a file; returns its web URL."""
        path = quote(f"{folder}/{filename}")
        r = self._req("PUT", f"/sites/{self.site_id()}/drive/root:/{path}:/content", content=content)
        return r.json().get("webUrl", "")

    def download(self, folder: str, filename: str) -> bytes | None:
        path = quote(f"{folder}/{filename}")
        try:
            return self._req("GET", f"/sites/{self.site_id()}/drive/root:/{path}:/content").content
        except Exception as e:
            if "404" in str(e) or "itemNotFound" in str(e):
                return None
            raise

    def exists(self, folder: str, filename: str) -> bool:
        path = quote(f"{folder}/{filename}")
        try:
            self._req("GET", f"/sites/{self.site_id()}/drive/root:/{path}")
            return True
        except Exception as e:
            if "404" in str(e) or "itemNotFound" in str(e):
                return False
            raise

    def folder_url(self, folder: str) -> str:
        try:
            return self._req("GET", f"/sites/{self.site_id()}/drive/root:/{quote(folder)}").json().get("webUrl", "")
        except Exception:
            return ""

    # ---- mail -------------------------------------------------------------
    def send_mail(self, sender: str, to: list[str], subject: str, html: str,
                  attachments: list[tuple[str, bytes]] = ()) -> None:
        msg = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "attachments": [{
                "@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                "contentBytes": base64.b64encode(data).decode(),
            } for name, data in attachments],
        }
        self._req("POST", f"/users/{sender}/sendMail", json={"message": msg, "saveToSentItems": True})


class Teams:
    """Posts to the HQ General channel via the 'Send webhook alerts to a channel' workflow."""

    def __init__(self, webhook_url: str):
        self.url = webhook_url
        self.http = httpx.Client(timeout=30)

    def post(self, title: str, lines: list[str], link: tuple[str, str] | None = None,
             links: list[tuple[str, str]] = ()) -> None:
        if not self.url:
            return
        body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
        body += [{"type": "TextBlock", "text": l, "wrap": True, "spacing": "Small"} for l in lines]
        card = {"type": "AdaptiveCard", "version": "1.4",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "body": body}
        buttons = [l for l in [*links, link] if l and l[1]]
        if buttons:
            card["actions"] = [{"type": "Action.OpenUrl", "title": t, "url": u} for t, u in buttons]
        payload = {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
        request(self.http, "Teams", "POST", self.url, json=payload)
