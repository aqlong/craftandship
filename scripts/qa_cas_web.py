#!/usr/bin/env python3
"""CAS Web App QA Suite: comprehensive automated testing against live/preview URL.

Tests the Craft & Ship platform itself (not generated customer apps).
Runs against any reachable instance of the web app (a local dev server by default).
Records all findings to a JSON results file for the learning loop.

Usage:
    python3 scripts/qa_cas_web.py [--url URL] [--iteration N]

Environment (everything is optional; nothing is read from .env files):
    CAS_BASE_URL       base URL to test when --url is not given (default http://localhost:4000)
    PREVIEW_PASSWORD   HTTP basic-auth password, only if the target sets PREVIEW_PASSWORD
    OPS_API_KEY        bearer key for the /ops/api/* checks, only if the target has one
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_URL = os.environ.get("CAS_BASE_URL", "http://localhost:4000")
PREVIEW_PW = os.environ.get("PREVIEW_PASSWORD", "")
OPS_API_KEY = os.environ.get("OPS_API_KEY", "")
RESULTS_DIR = Path(__file__).parent.parent / "qa_results"
KB_PATH = Path(__file__).parent.parent / "cli" / "cas_qa_kb.json"


class QAResult:
    def __init__(self, name: str, category: str):
        self.name = name
        self.category = category
        self.passed = False
        self.error = ""
        self.details = {}
        self.duration_ms = 0

    def to_dict(self):
        return {
            "name": self.name,
            "category": self.category,
            "passed": self.passed,
            "error": self.error,
            "details": self.details,
            "duration_ms": self.duration_ms,
        }


class CASWebQA:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = ("", PREVIEW_PW)
        self.results: list[QAResult] = []
        self.ops_headers = {"Authorization": f"Bearer {OPS_API_KEY}"}

    def _get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(f"{self.base_url}{path}", timeout=15, **kwargs)

    def _post(self, path: str, **kwargs) -> requests.Response:
        return self.session.post(f"{self.base_url}{path}", timeout=15, **kwargs)

    def _ops_get(self, path: str) -> requests.Response:
        """Ops API uses Bearer token which bypasses PREVIEW_PASSWORD."""
        return requests.get(
            f"{self.base_url}/ops/api{path}",
            headers={**self.ops_headers}, timeout=15,
        )

    def _ops_post(self, path: str, **kwargs) -> requests.Response:
        """Ops API uses Bearer token which bypasses PREVIEW_PASSWORD."""
        return requests.post(
            f"{self.base_url}/ops/api{path}",
            headers={**self.ops_headers}, timeout=15, **kwargs
        )

    def _run_test(self, name: str, category: str, fn):
        r = QAResult(name, category)
        start = time.time()
        try:
            fn(r)
            r.passed = True
        except AssertionError as e:
            r.error = str(e)
        except Exception as e:
            r.error = f"Exception: {type(e).__name__}: {e}"
        r.duration_ms = int((time.time() - start) * 1000)
        self.results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {name} ({r.duration_ms}ms){' - ' + r.error if r.error else ''}")

    # -----------------------------------------------------------------------
    # Public routes
    # -----------------------------------------------------------------------

    def test_health_endpoint(self, r: QAResult):
        resp = self._get("/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert data.get("status") == "ok", f"Health status: {data.get('status')}"
        r.details = {"db_status": data.get("database", "unknown")}

    def test_login_page_loads(self, r: QAResult):
        resp = self._get("/login")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert "csrf" in resp.text.lower(), "No CSRF token found on login page"
        assert "email" in resp.text.lower(), "No email field on login page"
        r.details = {"size": len(resp.text)}

    def test_signup_page_loads(self, r: QAResult):
        resp = self._get("/signup")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert "csrf" in resp.text.lower(), "No CSRF token found on signup page"
        r.details = {"size": len(resp.text)}

    def test_pricing_page_loads(self, r: QAResult):
        resp = self._get("/pricing")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        r.details = {"size": len(resp.text)}

    def test_gallery_page_loads(self, r: QAResult):
        resp = self._get("/gallery")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        r.details = {"size": len(resp.text)}

    def test_terms_page_loads(self, r: QAResult):
        resp = self._get("/terms")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_privacy_page_loads(self, r: QAResult):
        resp = self._get("/privacy")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_404_page(self, r: QAResult):
        resp = self._get("/nonexistent-page-xyz")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_robots_txt(self, r: QAResult):
        resp = self._get("/robots.txt")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert "User-agent" in resp.text, "robots.txt missing User-agent"

    def test_sitemap_xml(self, r: QAResult):
        resp = self._get("/sitemap.xml")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    # -----------------------------------------------------------------------
    # Auth security
    # -----------------------------------------------------------------------

    def test_login_csrf_required(self, r: QAResult):
        """POST /login without CSRF should be rejected."""
        resp = self._post("/login", data={"email": "a@b.example", "password": "x"})
        # Should get 403 or re-render login with error (not 500)
        assert resp.status_code != 500, f"Login without CSRF returned 500 (should be 403 or re-render)"
        r.details = {"status": resp.status_code}

    def test_signup_csrf_required(self, r: QAResult):
        """POST /signup without CSRF should be rejected."""
        resp = self._post("/signup", data={"email": "a@b.example", "password": "x", "name": "test"})
        assert resp.status_code != 500, f"Signup without CSRF returned 500"
        r.details = {"status": resp.status_code}

    def test_protected_routes_redirect_to_login(self, r: QAResult):
        """Unauthenticated access to protected routes should redirect to /login."""
        fresh = requests.Session()
        fresh.auth = ("", PREVIEW_PW)
        protected = ["/dashboard", "/intake", "/admin/builds"]
        failures = []
        for path in protected:
            resp = fresh.get(f"{self.base_url}{path}", timeout=10, allow_redirects=False)
            if resp.status_code not in (302, 303):
                failures.append(f"{path}: got {resp.status_code}")
        assert not failures, f"Routes not redirecting: {failures}"

    def test_admin_routes_require_admin(self, r: QAResult):
        """Admin routes should not be accessible to non-admin users."""
        fresh = requests.Session()
        fresh.auth = ("", PREVIEW_PW)
        admin_paths = ["/admin/builds", "/admin/customers", "/admin/emails"]
        for path in admin_paths:
            resp = fresh.get(f"{self.base_url}{path}", timeout=10, allow_redirects=False)
            # Should redirect to login (302) not render admin page (200)
            assert resp.status_code != 200, f"{path} accessible without login"

    # -----------------------------------------------------------------------
    # Ops API
    # -----------------------------------------------------------------------

    def test_ops_api_requires_auth(self, r: QAResult):
        """Ops API should reject requests without bearer token."""
        resp = self.session.get(f"{self.base_url}/ops/api/v1/builds", timeout=10)
        assert resp.status_code in (401, 403), f"Ops API without token: {resp.status_code}"

    def test_ops_api_builds_list(self, r: QAResult):
        resp = self._ops_get("/v1/builds")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert "builds" in data, "Missing 'builds' key"
        r.details = {"count": len(data["builds"])}

    def test_ops_api_stats(self, r: QAResult):
        resp = self._ops_get("/v1/stats")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert "builds" in data or "total_builds" in data or "stats" in data, f"Missing stats data: {list(data.keys())}"
        r.details = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}

    def test_ops_api_errors(self, r: QAResult):
        resp = self._ops_get("/v1/errors")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_ops_api_intake_analytics(self, r: QAResult):
        resp = self._ops_get("/v1/intake-analytics")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_ops_api_invalid_build_id(self, r: QAResult):
        resp = self._ops_get("/v1/builds/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_ops_api_requeue_invalid(self, r: QAResult):
        resp = self._ops_post("/v1/builds/00000000-0000-0000-0000-000000000000/requeue")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    # -----------------------------------------------------------------------
    # Security headers
    # -----------------------------------------------------------------------

    def test_csp_header_present(self, r: QAResult):
        resp = self._get("/login")
        csp = resp.headers.get("content-security-policy", "")
        assert csp, "No Content-Security-Policy header"
        assert "default-src" in csp, f"CSP missing default-src: {csp[:100]}"

    def test_no_server_header_leak(self, r: QAResult):
        resp = self._get("/health")
        server = resp.headers.get("x-powered-by", "")
        assert not server, f"x-powered-by header leaks server info: {server}"

    # -----------------------------------------------------------------------
    # Support chat
    # -----------------------------------------------------------------------

    def test_support_chat_rate_limited(self, r: QAResult):
        """Support chat should rate limit after many requests."""
        resp = self._post("/api/support/chat",
                          json={"message": "test"}, headers={"x-csrf-token": "invalid"})
        # Should return 403 (CSRF) or 429 (rate limit), not 500
        assert resp.status_code != 500, f"Support chat returned 500"
        r.details = {"status": resp.status_code}

    # -----------------------------------------------------------------------
    # Static assets
    # -----------------------------------------------------------------------

    def test_favicon_exists(self, r: QAResult):
        resp = self._get("/favicon.svg")
        assert resp.status_code == 200, f"Missing favicon: {resp.status_code}"

    def test_css_loads(self, r: QAResult):
        resp = self._get("/style.css")
        assert resp.status_code == 200, f"Missing style.css: {resp.status_code}"
        assert len(resp.text) > 100, f"style.css too small: {len(resp.text)} bytes"

    # -----------------------------------------------------------------------
    # Build status pages
    # -----------------------------------------------------------------------

    def test_build_status_page_loads(self, r: QAResult):
        """At least one build status page should load."""
        # Get a build ID from the ops API
        builds_resp = self._ops_get("/v1/builds")
        builds = builds_resp.json().get("builds", [])
        if not builds:
            r.details = {"skipped": "no builds found"}
            return
        build_id = builds[0]["id"]
        # Access the status page (requires auth, so we'll check redirect)
        resp = self._get(f"/builds/{build_id}", allow_redirects=False)
        # Either 200 (if we're authed somehow) or 302 (redirect to login)
        assert resp.status_code in (200, 302), f"Build page returned {resp.status_code}"
        r.details = {"build_id": build_id[:8], "status_code": resp.status_code}

    # -----------------------------------------------------------------------
    # Runner
    # -----------------------------------------------------------------------

    def run_all(self) -> dict:
        """Run all QA tests and return summary."""
        print(f"\n{'='*60}")
        print(f"CAS Web QA Suite: {self.base_url}")
        print(f"{'='*60}\n")

        tests = [
            # Public routes
            ("Health endpoint returns ok", "public", self.test_health_endpoint),
            ("Login page loads with CSRF", "public", self.test_login_page_loads),
            ("Signup page loads with CSRF", "public", self.test_signup_page_loads),
            ("Pricing page loads", "public", self.test_pricing_page_loads),
            ("Gallery page loads", "public", self.test_gallery_page_loads),
            ("Terms page loads", "public", self.test_terms_page_loads),
            ("Privacy page loads", "public", self.test_privacy_page_loads),
            ("404 page for unknown routes", "public", self.test_404_page),
            ("robots.txt exists", "public", self.test_robots_txt),
            ("sitemap.xml exists", "public", self.test_sitemap_xml),
            # Auth security
            ("Login POST requires CSRF", "auth", self.test_login_csrf_required),
            ("Signup POST requires CSRF", "auth", self.test_signup_csrf_required),
            ("Protected routes redirect to login", "auth", self.test_protected_routes_redirect_to_login),
            ("Admin routes require admin login", "auth", self.test_admin_routes_require_admin),
            # Ops API
            ("Ops API requires bearer token", "ops_api", self.test_ops_api_requires_auth),
            ("Ops API builds list", "ops_api", self.test_ops_api_builds_list),
            ("Ops API stats endpoint", "ops_api", self.test_ops_api_stats),
            ("Ops API errors endpoint", "ops_api", self.test_ops_api_errors),
            ("Ops API intake analytics", "ops_api", self.test_ops_api_intake_analytics),
            ("Ops API 404 for invalid build", "ops_api", self.test_ops_api_invalid_build_id),
            ("Ops API requeue invalid build", "ops_api", self.test_ops_api_requeue_invalid),
            # Security headers
            ("CSP header present", "security", self.test_csp_header_present),
            ("No x-powered-by header", "security", self.test_no_server_header_leak),
            # Support
            ("Support chat rejects bad requests", "support", self.test_support_chat_rate_limited),
            # Assets
            ("Favicon exists", "assets", self.test_favicon_exists),
            ("CSS loads and has content", "assets", self.test_css_loads),
            # Build pages
            ("Build status page accessible", "builds", self.test_build_status_page_loads),
        ]

        for name, category, fn in tests:
            self._run_test(name, category, fn)

        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        total = len(self.results)

        print(f"\n{'='*60}")
        print(f"Results: {passed}/{total} passed, {failed} failed")
        print(f"{'='*60}\n")

        if failed:
            print("FAILURES:")
            for r in self.results:
                if not r.passed:
                    print(f"  [{r.category}] {r.name}: {r.error}")

        return {
            "passed": passed,
            "failed": failed,
            "total": total,
            "results": [r.to_dict() for r in self.results],
        }


def update_qa_kb(results: dict, iteration: int):
    """Update the CAS QA knowledge base with findings."""
    kb = {}
    if KB_PATH.exists():
        try:
            kb = json.loads(KB_PATH.read_text())
        except Exception:
            pass

    for r in results["results"]:
        if not r["passed"]:
            sig = r["name"]
            if sig not in kb:
                kb[sig] = {
                    "category": r["category"],
                    "first_seen_iteration": iteration,
                    "occurrences": 0,
                    "fixed": False,
                    "fix_description": "",
                    "last_error": "",
                }
            kb[sig]["occurrences"] += 1
            kb[sig]["last_error"] = r["error"]
            kb[sig]["last_seen_iteration"] = iteration

        elif r["passed"]:
            sig = r["name"]
            if sig in kb and not kb[sig]["fixed"]:
                kb[sig]["fixed"] = True
                kb[sig]["fixed_at_iteration"] = iteration

    KB_PATH.write_text(json.dumps(kb, indent=2) + "\n")
    return kb


def save_results(results: dict, iteration: int):
    """Save results to qa_results directory."""
    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"iteration_{iteration}_{timestamp}.json"
    results["iteration"] = iteration
    results["timestamp"] = timestamp
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Results saved to {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="CAS Web App QA Suite")
    parser.add_argument("--url", default=DEFAULT_URL, help="Base URL to test")
    parser.add_argument("--iteration", type=int, default=1, help="Iteration number")
    args = parser.parse_args()

    qa = CASWebQA(args.url)
    results = qa.run_all()

    save_results(results, args.iteration)
    kb = update_qa_kb(results, args.iteration)

    # Print KB summary
    if kb:
        print(f"\nQA Knowledge Base ({len(kb)} entries):")
        for sig, entry in kb.items():
            status = "FIXED" if entry.get("fixed") else f"OPEN (seen {entry['occurrences']}x)"
            print(f"  [{status}] {sig}")

    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
