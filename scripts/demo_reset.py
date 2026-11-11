"""Wipe and reseed the synthetic demo data so a run starts clean.

Calls the app's own POST /api/v1/demo/reset on a running instance, which
deletes every demo row but keeps the seed org/person/setup, then verifies the
wipe (no purchases left) and that the scenario catalog still answers. Works
identically against a local dev server and App Service::

    make demo-reset
    make demo-reset DEMO_BASE_URL=https://ramphack-affan.azurewebsites.net

The target app must already be running (``make demo-local`` for the local
SQLite backup). Only synthetic demo data is ever touched.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, OpenerDirector

BASE_ENV_VAR = "DEMO_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

_HEADERS = {"X-Requested-With": "taxleak", "Content-Type": "application/json"}


def _request(
    opener: OpenerDirector,
    method: str,
    url: str,
    body: object,
    key: str,
    token: str | None,
) -> dict[str, object]:
    headers = dict(_HEADERS, **{"Idempotency-Key": key})
    if token is not None:
        headers["X-CSRF-Token"] = token
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code} on {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach {url}: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Unexpected non-object response from {url}: {payload!r}")
    return payload


def _get(opener: OpenerDirector, url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code} on {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach {url}: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Unexpected non-object response from {url}: {payload!r}")
    return payload


def reset(base_url: str, key: str) -> None:
    """Log in, wipe demo rows, and verify the run starts clean."""
    base = base_url.rstrip("/")
    opener = urllib.request.build_opener(HTTPCookieProcessor(CookieJar()))

    login = _request(
        opener, "POST", f"{base}/api/v1/demo/login", {}, f"{key}-login", None
    )
    token = login.get("csrf_token")
    if not isinstance(token, str) or not token:
        raise SystemExit(f"Login did not return a CSRF token: {login!r}")

    result = _request(opener, "POST", f"{base}/api/v1/demo/reset", {}, key, token)
    if result.get("reset") is not True:
        raise SystemExit(f"Reset did not confirm: {result!r}")

    purchases = _get(opener, f"{base}/api/v1/purchases")
    remaining = purchases.get("purchases")
    if not isinstance(remaining, list) or remaining:
        raise SystemExit(f"Reset left purchases behind: {remaining!r}")
    scenarios = _get(opener, f"{base}/api/v1/demo/scenarios")
    names = scenarios.get("scenarios")
    if not isinstance(names, list) or "case1" not in names:
        raise SystemExit(f"Scenario catalog missing after reset: {names!r}")
    print(f"Demo reset complete at {base}: 0 purchases, {len(names)} scenarios ready.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get(BASE_ENV_VAR, DEFAULT_BASE_URL),
        help=f"App base URL (default: ${BASE_ENV_VAR} or {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--key",
        default="demo-reset",
        help="Idempotency-Key for the reset (default: demo-reset)",
    )
    args = parser.parse_args(argv)
    reset(args.base_url, args.key)


if __name__ == "__main__":
    main(sys.argv[1:])
