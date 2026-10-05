"""Shared HTTP helper: retries on rate limits and temporary errors."""
from __future__ import annotations

import time

import httpx


class ApiError(Exception):
    def __init__(self, service: str, response: httpx.Response):
        self.status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]
        self.body = body
        super().__init__(f"{service} {response.request.method} {response.request.url.path} "
                         f"-> {response.status_code}: {str(body)[:500]}")


def request(client: httpx.Client, service: str, method: str, url: str, *,
            attempts: int = 4, **kwargs) -> httpx.Response:
    delay = 2.0
    for i in range(attempts):
        resp = client.request(method, url, **kwargs)
        if resp.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
            wait = float(resp.headers.get("Retry-After", delay))
            time.sleep(min(wait, 60))
            delay *= 2
            continue
        if resp.status_code >= 400:
            raise ApiError(service, resp)
        return resp
    raise RuntimeError("unreachable")
