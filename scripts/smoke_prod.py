#!/usr/bin/env python3
"""Production/staging smoke tests for CAS.

Hits key CAS endpoints and verifies HTTP responses. Includes one
authenticated endpoint to catch broken auth middleware.

Usage:
    CAS_PROD_URL=https://your-app.example.com python3 scripts/smoke_prod.py --env production
    CAS_STAGING_URL=https://staging.example.com python3 scripts/smoke_prod.py --env staging

Environment:
    CAS_PROD_URL / CAS_STAGING_URL   base URL for the chosen --env (required; no default host)
    SMOKE_TEST_EMAIL / SMOKE_TEST_PASSWORD   optional; enables the authenticated login check
"""
import argparse
import json
import os
import sys

import requests

PROD_URL = os.environ.get("CAS_PROD_URL", "")
STAGING_URL = os.environ.get("CAS_STAGING_URL", "")

# Timeout for each request (seconds)
REQUEST_TIMEOUT = 15


def smoke(env: str = "production") -> dict:
    """Hit key CAS endpoints and verify responses.

    Returns: {"passed": bool, "checks": [{"name", "url", "ok", "status", "error"}]}
    """
    base = PROD_URL if env == "production" else STAGING_URL
    if not base:
        return {"passed": False, "checks": [{"name": "config", "url": "", "ok": False,
                "error": f"No URL configured for {env}"}]}

    base = base.rstrip("/")

    checks_config = [
        ("health", f"{base}/health", 200),
        ("homepage", f"{base}/", 200),
        ("pricing", f"{base}/pricing", 200),
        ("gallery", f"{base}/gallery", 200),
        ("login", f"{base}/login", 200),
        ("how-it-works", f"{base}/how-it-works", 200),
    ]

    results = []
    session = requests.Session()

    for name, url, expected_status in checks_config:
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            ok = resp.status_code == expected_status
            results.append({
                "name": name,
                "url": url,
                "ok": ok,
                "status": resp.status_code,
                "error": None if ok else f"Expected {expected_status}, got {resp.status_code}",
            })
        except Exception as e:
            results.append({
                "name": name,
                "url": url,
                "ok": False,
                "status": None,
                "error": str(e),
            })

    # Authenticated endpoint check: POST /login with test creds
    # Should get a 302 redirect (successful login) or at minimum not a 500
    test_email = os.environ.get("SMOKE_TEST_EMAIL", "")
    test_password = os.environ.get("SMOKE_TEST_PASSWORD", "")
    if test_email and test_password:
        login_url = f"{base}/login"
        try:
            # First get CSRF token from login page
            login_page = session.get(login_url, timeout=REQUEST_TIMEOUT)
            csrf_token = ""
            if 'name="_csrf"' in login_page.text:
                import re
                match = re.search(r'name="_csrf"\s+value="([^"]+)"', login_page.text)
                if match:
                    csrf_token = match.group(1)

            resp = session.post(
                login_url,
                data={"email": test_email, "password": test_password, "_csrf": csrf_token},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
            # Successful login redirects (302); auth failure shows 200 with error
            ok = resp.status_code in (302, 303)
            results.append({
                "name": "auth-login",
                "url": login_url,
                "ok": ok,
                "status": resp.status_code,
                "error": None if ok else f"Login returned {resp.status_code} (expected 302)",
            })
        except Exception as e:
            results.append({
                "name": "auth-login",
                "url": login_url,
                "ok": False,
                "status": None,
                "error": str(e),
            })

    passed = all(r["ok"] for r in results)
    return {"passed": passed, "checks": results}


def main():
    parser = argparse.ArgumentParser(description="CAS smoke tests")
    parser.add_argument("--env", choices=["production", "staging"], default="production")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    result = smoke(args.env)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for check in result["checks"]:
            status = "PASS" if check["ok"] else "FAIL"
            detail = check.get("error", "") or f"HTTP {check['status']}"
            print(f"  [{status}] {check['name']}: {detail}")
        print()
        if result["passed"]:
            print(f"Smoke tests PASSED ({args.env})")
        else:
            print(f"Smoke tests FAILED ({args.env})")

    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
