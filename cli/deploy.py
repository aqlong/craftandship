# cli/deploy.py - Railway deployment pipeline

"""Railway deployment helpers. Unlike the rest of the v1 pipeline (legacy, frozen,
superseded by compose/), this module is still used by the v2 worker: deploy(),
verify_health(), verify_render() and the GraphQL helpers. verify_routes(),
verify_auth() and verify_data() exist for the v1 engine and are skipped for v2.
See cli/README.md."""

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

# The URL guard lives in monitor/, a top-level package. The worker and the test
# suite already have the repo root on sys.path; running this file as a script
# (python cli/deploy.py, cli/railway_cleanup.py) does not.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from coordinator.envflags import safety_switch
from monitor.bounded_fetch import provider_post
from monitor.url_redaction import has_url_credentials, redact_url_credentials
from monitor.ping import (
    PAGE_BODY_MAX_BYTES,
    guarded_session,
    host_resolution_problem,
    probe_url_problem,
)

load_dotenv()

logger = logging.getLogger(__name__)

RAILWAY_API = "https://backboard.railway.app/graphql/v2"
MAX_RETRIES = 5
DEPLOY_TIMEOUT = 300  # seconds
# Deadline of ONE request to the Railway API, whole exchange (lookup, connect,
# TLS, request, answer). gql() makes at most MAX_RETRIES of them with waits of
# 1, 2, 4 and 8 seconds in between, so one gql() call ends within
# MAX_RETRIES * RAILWAY_CALL_DEADLINE_S + 15 seconds.
RAILWAY_CALL_DEADLINE_S = 30

POSTGRES_USER = "railway"
POSTGRES_DB = "railway"

# The Postgres major version of the database every customer app gets
# (provision_postgres creates the service from this image). It is stated here
# once. pg_dump refuses a server newer than itself, so the client in the worker
# image (Dockerfile.worker, POSTGRES_CLIENT_MAJOR) must be at least this major,
# which tests/test_postgres_client_version.py checks by reading both files, and
# monitor/backup.py compares the installed pg_dump with the server it is about
# to dump before it dumps.
POSTGRES_SERVER_MAJOR = 15
POSTGRES_IMAGE = f"postgres:{POSTGRES_SERVER_MAJOR}"

# The mail key a generated customer app receives, if any. It is a separate
# variable on purpose: see _generated_app_mail_env().
GENERATED_APP_MAIL_KEY_ENV = "GENERATED_APP_RESEND_API_KEY"
# The sending domain that belongs to that key (verified with the mail
# provider), handed to the app as RESEND_FROM_DOMAIN.
GENERATED_APP_MAIL_DOMAIN_ENV = "GENERATED_APP_RESEND_FROM_DOMAIN"


def magic_link_auth_enabled() -> bool:
    """USE_MAGIC_LINK_AUTH: on unless set to an explicit OFF word.

    Read at call time. cli/build.py and cli/test.py call this function, and
    the worker (queue/worker_pg.py) parses the variable with the same
    safety_switch, so every reader gives one answer for any spelling.
    """
    return safety_switch("USE_MAGIC_LINK_AUTH")


# Regex for a safe project/service name: alphanumeric + hyphens, 1-50 chars.
# Used by sanitize_app_name() to strip unsafe characters before passing to the
# Railway API, preventing unexpected input from influencing API calls.
_APP_NAME_RE = re.compile(r"[^a-zA-Z0-9-]")


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

def sanitize_app_name(name: str) -> str:
    """Return a Railway-safe project/service name.

    Strips every character that is not alphanumeric or a hyphen, collapses
    consecutive hyphens, trims leading/trailing hyphens, and truncates to 50
    characters.  Raises ValueError when the result is empty.

    Args:
        name: Raw app name string (e.g. from package.json or spec).

    Returns:
        A sanitized, non-empty name string.

    Raises:
        ValueError: If sanitization produces an empty string.
    """
    # Replace unsafe chars with hyphens so words remain separated
    sanitized = _APP_NAME_RE.sub("-", name)
    # Collapse consecutive hyphens into one
    sanitized = re.sub(r"-+", "-", sanitized)
    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-")
    # Enforce max length
    sanitized = sanitized[:50]
    if not sanitized:
        raise ValueError(f"app_name '{name}' produced an empty string after sanitization")
    return sanitized


def redact_url(url: str) -> str:
    """Replace the credentials of every URL in `url` with '***' for safe logging.

    The credentials are everything between "://" and the last "@" of the URL's
    authority: user and password, a token that is the whole userinfo, a user
    name that is itself the secret, a password that holds "@" or ")". The rule
    and its limits are in monitor/url_redaction.py; it is the same rule as the
    web app's redactForCustomer. The scheme is not needed and not limited to
    PostgreSQL's spellings: the same function redacts log lines and error
    text, where a Redis, AMQP, Git or proxy URL with credentials is just as
    unwelcome. The scan is linear in the length of the text (this runs over
    provider error text of up to a few megabytes).

    Examples:
        postgresql://user:<pw>@host:5432/db  ->  postgresql://***@host:5432/db
        https://<token>@host/repo.git        ->  https://***@host/repo.git

    Args:
        url: A connection URL, or any text that may contain one.

    Returns:
        The text with each userinfo replaced by '***', or the original string
        unchanged if it holds no URL with a userinfo.
    """
    return redact_url_credentials(url)


# ---------------------------------------------------------------------------
# Secrets hygiene: child process environments and error text
# ---------------------------------------------------------------------------

# Variables a child process must not inherit from the worker: platform API
# keys, Railway/Stripe/Resend/GitHub credentials and the worker's own database
# URL. Used for the Railway CLI here and, through cli/test.py and cli/qa.py,
# for everything that runs model-written code (npm install, the app under
# test, psql on generated SQL, generated QA tests). Callers set back only what
# the child needs.
_SECRET_ENV_NAME_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?key|secret|token|passw|credential|private[_-]?key|dsn)")
_SECRET_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "RAILWAY_", "STRIPE_", "RESEND_", "GITHUB_", "SENTRY_", "OPS_")
_SECRET_ENV_EXACT = frozenset({"DATABASE_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"})


def env_without_secrets(base=None) -> dict:
    """Copy of `base` (default os.environ) minus credential-bearing variables.

    This keeps secrets out of a child's environment. It is not a sandbox: a
    child that runs as the same user in the same container can still read the
    worker's files and, on Linux, the worker's /proc/<pid>/environ.
    """
    source = os.environ if base is None else base
    return {
        name: value
        for name, value in source.items()
        if name not in _SECRET_ENV_EXACT
        and not name.upper().startswith(_SECRET_ENV_PREFIXES)
        and not _SECRET_ENV_NAME_RE.search(name)
        and not has_url_credentials(str(value))   # whatever the variable is called (REDIS_URL, a proxy, a Git remote)
    }


_SENSITIVE_VARIABLE_NAME_RE = re.compile(r"(?i)(secret|token|passw|api[_-]?key|database_url|dsn|credential)")
_MAX_ERROR_TEXT_CHARS = 1000


def _url_password(value: str):
    """Password embedded in a URL-shaped string, or None."""
    try:
        return urlsplit(value).password
    except ValueError:
        return None


def _sensitive_values(variables, token=None) -> set:
    """Strings that must not appear in text built from a Railway response.

    A GraphQL server can echo the variables of a rejected request back inside
    its error message (for example "Variable $input got invalid value {...}").
    The variables of variableCollectionUpsert are the customer app's
    DATABASE_URL, SESSION_SECRET and mail key, and every request carries the
    API token, so each of those is removed from error text by exact value.
    """
    found = set()
    if token:
        found.add(str(token))

    def _walk(node, name=""):
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, str(key))
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item, name)
        elif isinstance(node, str) and len(node) >= 8 and _SENSITIVE_VARIABLE_NAME_RE.search(name):
            found.add(node)
            password = _url_password(node)
            if password:
                found.add(password)

    _walk(variables)
    return found


def _scrub(text, sensitive=(), limit=_MAX_ERROR_TEXT_CHARS) -> str:
    """`text` as a bounded string with every value in `sensitive` and any
    password inside a database URL replaced by ***."""
    out = text if isinstance(text, str) else str(text)
    values = [v for v in sensitive if isinstance(v, str) and v]
    for value in sorted(values, key=len, reverse=True):
        out = out.replace(value, "***")
    out = redact_url(out)
    if len(out) > limit:
        out = out[:limit] + "...(truncated)"
    return out


# ---------------------------------------------------------------------------
# Auth and HTTP
# ---------------------------------------------------------------------------

class DeployConfigError(RuntimeError):
    """A deploy cannot start because of how this machine is set up: no
    RAILWAY_API_TOKEN, or no working Railway CLI.

    Raised instead of exiting the process: this module is imported by the
    worker, the monitor and the watchdog probe, and only a command-line entry
    point may decide to exit (main() below prints the message and does).
    """


def get_token():
    """Return RAILWAY_API_TOKEN from the environment.

    Reads the token from the environment only - never hardcoded.

    Raises:
        DeployConfigError: The variable is unset or empty.
    """
    token = os.getenv("RAILWAY_API_TOKEN")
    if not token:
        raise DeployConfigError(
            "RAILWAY_API_TOKEN is not set. Create a token at "
            "https://railway.app/account/tokens and put it in the environment."
        )
    return token


def check_railway_cli():
    """Ensure the Railway CLI is installed and answers `railway --version`.

    Uses a list-form subprocess call (no shell=True) to avoid shell injection.

    Raises:
        DeployConfigError: The CLI is missing, exits non-zero or does not
            answer within 10 seconds.
    """
    try:
        result = subprocess.run(
            ["railway", "--version"],
            capture_output=True,
            text=True,
            timeout=10,  # don't hang if CLI is broken
            env=env_without_secrets(),
        )
        usable = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        usable = False
    if not usable:
        raise DeployConfigError(
            "Railway CLI not found or not working. Install it with: npm install -g @railway/cli"
        )


class RailwayOutcomeUnknown(RuntimeError):
    """A mutation was sent to Railway and no usable answer came back.

    Railway may or may not have applied it. gql() does not send such a
    mutation again (see there); the caller decides what an unknown outcome
    means for it.
    """


_OPERATION_RE = re.compile(r"\s*(query|mutation)\b\s*(\w+)?")


def _operation(query) -> tuple:
    """("query" or "mutation", operation name or "") of a GraphQL document."""
    match = _OPERATION_RE.match(query or "")
    if not match:
        return "query", ""   # the shorthand "{ ... }" form is a query
    return match.group(1), match.group(2) or ""


def gql(query, variables=None, token=None, *, repeatable=False, deadline=None):
    """Execute a Railway GraphQL request.

    All data is passed as structured JSON (never string-interpolated into the
    query), so there is no GraphQL injection risk.  The API token is sent only
    in the Authorization header - never logged.  Error text taken from the
    response is passed through _scrub() first, because a rejected request can
    come back with its own variables (database URL, session secret) quoted in
    the message, and callers log and store these errors.

    Retries, with exponential backoff, up to MAX_RETRIES attempts in all:
      * a query, and a mutation the caller declares `repeatable` (sending it
        twice leaves the same state as sending it once), on a 429, a 5xx or a
        network error;
      * any other mutation only where the request cannot have been applied: a
        429, or a connection that was never established.
    Such a mutation is NOT sent again after a 5xx, a read timeout or a
    connection that broke off: Railway may already have applied it (created
    the project, the service, the domain), and a second send would create a
    second one that nothing records. That case raises RailwayOutcomeUnknown.

    Each attempt is one bounded exchange (monitor.bounded_fetch.provider_post)
    of at most RAILWAY_CALL_DEADLINE_S seconds and a few megabytes of answer.
    A caller with a budget of its own passes `deadline`: no attempt runs past
    it and none is started after it.

    Args:
        query: GraphQL query/mutation string.
        variables: Optional dict of GraphQL variables.
        token: Railway API token; fetched from environment if None.
        repeatable: True for a mutation that is safe to send more than once.
        deadline: Optional time.monotonic() value that bounds the whole call,
            retries and waits included.

    Returns:
        The ``data`` portion of the GraphQL JSON response.

    Raises:
        RuntimeError: On 4xx client errors or GraphQL-level errors (no retry).
        RailwayOutcomeUnknown: A non-repeatable mutation with no usable answer.
        requests.RequestException: After MAX_RETRIES network failures.
    """
    if token is None:
        token = get_token()
    kind, operation_name = _operation(query)
    may_resend = kind == "query" or repeatable
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    sensitive = _sensitive_values(variables, token)

    def _seconds_left():
        return None if deadline is None else deadline - time.monotonic()

    for attempt in range(MAX_RETRIES):
        left = _seconds_left()
        if left is not None and left <= 0:
            raise requests.exceptions.Timeout(
                f"Railway API call {operation_name or '(unnamed)'} ran out of time before attempt {attempt + 1}")
        call_deadline = RAILWAY_CALL_DEADLINE_S if left is None else min(RAILWAY_CALL_DEADLINE_S, left)
        try:
            response = provider_post(RAILWAY_API, json=payload, headers=headers, timeout=call_deadline)
            # 4xx = client/schema error; fail immediately without retry
            if 400 <= response.status_code < 500 and response.status_code != 429:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text
                raise RuntimeError(f"Railway API {response.status_code}: {_scrub(detail, sensitive)}")
            if response.status_code >= 500 and not may_resend:
                raise RailwayOutcomeUnknown(
                    f"Railway did not confirm the mutation {operation_name or '(unnamed)'}: "
                    f"it answered HTTP {response.status_code}. The request was not sent again, "
                    f"because Railway may already have applied it."
                )
            if response.status_code == 429 or response.status_code >= 500:
                wait = 2 ** attempt
                left = _seconds_left()
                if left is not None and left <= wait:
                    raise requests.exceptions.Timeout(
                        f"Railway API answered {response.status_code} and no time is left to retry")
                print(f"  Railway API {response.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                errors = data["errors"]
                first = errors[0] if isinstance(errors, list) and errors else errors
                message = first.get("message") if isinstance(first, dict) else first
                raise RuntimeError(f"GraphQL error: {_scrub(message, sensitive)}")
            return data["data"]
        except RuntimeError:
            raise  # never retry schema/logic errors
        except requests.RequestException as error:
            # ConnectTimeout: the connection was never established, so nothing
            # was sent. Every other failure of a mutation that is not
            # repeatable leaves its outcome unknown.
            if not may_resend and not isinstance(error, requests.exceptions.ConnectTimeout):
                raise RailwayOutcomeUnknown(
                    f"Railway did not confirm the mutation {operation_name or '(unnamed)'}: "
                    f"no answer arrived ({type(error).__name__}). The request was not sent again, "
                    f"because Railway may already have applied it."
                ) from None
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt
            left = _seconds_left()
            if left is not None and left <= wait:
                raise
            print(f"  Request failed ({_scrub(error, sensitive)}), retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError("Max retries exceeded")


# ---------------------------------------------------------------------------
# Project and service setup
# ---------------------------------------------------------------------------

def create_project(app_name, token):
    """Create a Railway project; return project_id.

    Args:
        app_name: Sanitized application name (alphanumeric + hyphens).
        token: Railway API token.

    Returns:
        The new project's ID string.
    """
    print(f"Creating project '{app_name}'...")
    asked_at = time.time()
    try:
        data = gql("""
            mutation ProjectCreate($input: ProjectCreateInput!) {
                projectCreate(input: $input) { id name }
            }
        """, {"input": {"name": app_name}}, token)
    except RailwayOutcomeUnknown as unknown:
        # If Railway did create the project, its id is known to nobody here:
        # deploy() has nothing to clean up and no build row will record it.
        # It is not adopted either. Project names are not unique (every
        # attempt and every rebuild of an app uses the same one), so "a
        # project with this name that is new" could be another attempt's.
        # The attempt fails, and the projects it could have left behind are
        # named for whoever reads the failure.
        unknown.unrecorded_project_candidates = _projects_named_since(app_name, asked_at, token)
        raise
    project_id = data["projectCreate"]["id"]
    print(f"  Project ID: {project_id}")
    return project_id


def _projects_named_since(app_name, since_epoch, token) -> list:
    """Ids of Railway projects called `app_name` that were created at or after
    `since_epoch` (less a minute of clock slack). Best effort: [] when the
    list cannot be read."""
    from datetime import datetime, timezone
    try:
        found = []
        for node in list_projects(token):
            if node.get("name") != app_name:
                continue
            created = datetime.fromisoformat(str(node.get("createdAt")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created.timestamp() >= since_epoch - 60:
                found.append(str(node.get("id")))
        return found
    except Exception:  # a listing that fails must not replace the error being reported
        return []


def get_environment_id(project_id, token):
    """Return the production environment ID for a project.

    Falls back to the first environment if no environment is named
    'production'.

    Args:
        project_id: Railway project ID.
        token: Railway API token.

    Returns:
        Environment ID string.
    """
    data = gql("""
        query Project($id: String!) {
            project(id: $id) {
                environments { edges { node { id name } } }
            }
        }
    """, {"id": project_id}, token)
    edges = data["project"]["environments"]["edges"]
    for edge in edges:
        if edge["node"]["name"] == "production":
            return edge["node"]["id"]
    return edges[0]["node"]["id"]


def create_service(project_id, app_name, token):
    """Create an app service in the project; return service_id.

    Args:
        project_id: Railway project ID.
        app_name: Sanitized application name.
        token: Railway API token.

    Returns:
        The new service's ID string.
    """
    print(f"Creating service '{app_name}'...")
    data = gql("""
        mutation ServiceCreate($input: ServiceCreateInput!) {
            serviceCreate(input: $input) { id name }
        }
    """, {"input": {"projectId": project_id, "name": app_name}}, token)
    service_id = data["serviceCreate"]["id"]
    print(f"  Service ID: {service_id}")
    return service_id


def provision_postgres(project_id, environment_id, token):
    """Create a PostgreSQL service with generated credentials; return (db_service_id, database_url).

    The Postgres password is generated with ``secrets.token_urlsafe`` (32 bytes
    of entropy) and is never printed to stdout.  The DATABASE_URL is returned
    for use as a Railway environment variable; callers must use
    ``redact_url()`` before logging it.

    Storage: the service is created from the ``POSTGRES_IMAGE`` image (postgres:15) and given
    ``PGDATA``; this function, and the rest of this module, never creates or
    attaches a Railway volume for it. Whether the data directory survives a
    restart or redeploy of that service therefore depends on how Railway
    treats a service without a volume, which this repository does not
    establish. The returned URL uses the private host name
    ``postgres.railway.internal``, the same for every customer app.

    Args:
        project_id: Railway project ID.
        environment_id: Railway environment ID.
        token: Railway API token.

    Returns:
        Tuple of (db_service_id, database_url).
    """
    print("Provisioning PostgreSQL...")
    data = gql("""
        mutation ServiceCreate($input: ServiceCreateInput!) {
            serviceCreate(input: $input) { id name }
        }
    """, {
        "input": {
            "projectId": project_id,
            "name": "postgres",
            "source": {"image": POSTGRES_IMAGE},
        }
    }, token)
    db_service_id = data["serviceCreate"]["id"]
    print(f"  Postgres service ID: {db_service_id}")

    # Generate a cryptographically random password; never logged.
    pg_password = secrets.token_urlsafe(32)
    set_env_vars(project_id, environment_id, db_service_id, {
        "POSTGRES_USER": POSTGRES_USER,
        "POSTGRES_PASSWORD": pg_password,
        "POSTGRES_DB": POSTGRES_DB,
        "PGDATA": "/var/lib/postgresql/data/pgdata",
    }, token)

    # Trigger a fresh deployment so it starts with the correct env vars.
    # Repeatable: a second redeploy of the fresh database service starts it
    # once more with the same variables.
    gql("""
        mutation Redeploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
    """, {"serviceId": db_service_id, "environmentId": environment_id}, token, repeatable=True)

    # Wait for the deployment to appear, then wait for it to succeed.
    print("  Waiting for Postgres to deploy...")
    deployment_id = None
    for _ in range(18):
        try:
            deployment_id = get_latest_deployment(db_service_id, environment_id, token)
        except RuntimeError:
            pass
        if deployment_id:
            break
        time.sleep(5)

    if deployment_id:
        wait_for_deployment(deployment_id, token, timeout=180)
    else:
        print("  Warning: no Postgres deployment found; proceeding with assumed credentials.")

    # Construct DATABASE_URL using Railway private networking.
    # The URL contains the pg_password secret - never log the raw URL.
    database_url = (
        f"postgresql://{POSTGRES_USER}:{pg_password}"
        f"@postgres.railway.internal:5432/{POSTGRES_DB}"
    )
    print("  Postgres ready.")
    return db_service_id, database_url


def set_env_vars(project_id, environment_id, service_id, variables, token):
    """Bulk-set environment variables on a service.

    Variable values are passed as structured JSON to the GraphQL API and are
    never logged.  Callers should treat the ``variables`` dict as sensitive.

    Args:
        project_id: Railway project ID.
        environment_id: Railway environment ID.
        service_id: Target service ID.
        variables: Dict of {name: value} environment variable pairs.
        token: Railway API token.
    """
    # Log only the count and keys - never log the values (they may be secrets).
    print(f"Setting {len(variables)} environment variable(s): {', '.join(variables.keys())}")
    # Repeatable: an upsert of the same names and values.
    gql("""
        mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
            variableCollectionUpsert(input: $input)
        }
    """, {
        "input": {
            "projectId": project_id,
            "environmentId": environment_id,
            "serviceId": service_id,
            "variables": variables,
        }
    }, token, repeatable=True)
    print("  Done.")


def generate_domain(environment_id, service_id, token):
    """Create a Railway-managed domain; return the domain string.

    Args:
        environment_id: Railway environment ID.
        service_id: Target service ID.
        token: Railway API token.

    Returns:
        Domain name string (without scheme).
    """
    print("Generating domain...")
    data = gql("""
        mutation ServiceDomainCreate($input: ServiceDomainCreateInput!) {
            serviceDomainCreate(input: $input) { domain }
        }
    """, {
        "input": {
            "environmentId": environment_id,
            "serviceId": service_id,
            "targetPort": 3000,
        }
    }, token)
    domain = data["serviceDomainCreate"]["domain"]
    print(f"  Domain: https://{domain}")
    return domain


def add_custom_domain(environment_id, service_id, custom_domain, token):
    """Attach a customer-owned domain to a Railway service.

    After this call, Railway returns the CNAME target that the customer must
    point their DNS record at.  The caller is responsible for printing
    DNS instructions to the user.

    Railway validates ownership via a CNAME record:
      <custom_domain>  CNAME  <cname_target>

    For apex domains (no subdomain), Railway requires an ALIAS or ANAME record;
    the customer's DNS provider must support this (Cloudflare, NS1, etc. do).

    Args:
        environment_id: Railway environment ID.
        service_id: Target service ID.
        custom_domain: The domain to attach (e.g. 'app.mybusiness.example').
        token: Railway API token.

    Returns:
        Dict with 'domain' and 'cnameTarget' keys, or None on failure.
    """
    # Basic validation: reject obviously unsafe domain strings before the API call.
    # A domain must contain at least one dot and only safe characters.
    if not custom_domain or not re.fullmatch(r"[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}", custom_domain):
        print(f"  Warning: '{custom_domain}' does not look like a valid domain; skipping.")
        return None

    print(f"Attaching custom domain '{custom_domain}'...")
    try:
        data = gql("""
            mutation CustomDomainCreate($input: CustomDomainCreateInput!) {
                customDomainCreate(input: $input) {
                    id
                    domain
                    cnameTarget: value
                }
            }
        """, {
            "input": {
                "environmentId": environment_id,
                "serviceId": service_id,
                "domain": custom_domain,
            }
        }, token)
        result = data["customDomainCreate"]
        cname_target = result.get("cnameTarget", "<check Railway dashboard>")
        print(f"  Custom domain attached: https://{result['domain']}")
        print(f"  DNS action required:")
        print(f"    Add a CNAME record for '{custom_domain}' pointing to '{cname_target}'")
        print(f"    (For apex domains use ALIAS/ANAME if your DNS provider supports it)")
        return result
    except RuntimeError as error:
        print(f"  Warning: could not attach custom domain: {error}")
        print(f"  You can add it manually in the Railway dashboard.")
        return None


# ---------------------------------------------------------------------------
# Source deployment
# ---------------------------------------------------------------------------

def create_project_token(project_id, environment_id, token):
    """Create a project-scoped token for Railway CLI auth.

    Project tokens are short-lived and scoped to a single project/environment.
    The returned token is a secret - callers must not log it.

    Args:
        project_id: Railway project ID.
        environment_id: Railway environment ID.
        token: Railway API token (personal/team token for creating project tokens).

    Returns:
        Project token string.
    """
    data = gql("""
        mutation ProjectTokenCreate($input: ProjectTokenCreateInput!) {
            projectTokenCreate(input: $input)
        }
    """, {
        "input": {
            "projectId": project_id,
            "environmentId": environment_id,
            "name": "deploy-cli-token",
        }
    }, token)
    return data["projectTokenCreate"]


def deploy_source(build_dir, project_id, environment_id, service_id, token):
    """Upload source via Railway CLI using a project token; return deployment_id if parseable.

    Uses list-form subprocess (no shell=True) to prevent shell injection.
    The project token is passed via environment variable, never via command-line
    arguments (where it would appear in process listings).

    Args:
        build_dir: Absolute path to the build directory to upload.
        project_id: Railway project ID.
        environment_id: Railway environment ID.
        service_id: Target service ID.
        token: Railway API token.

    Returns:
        Deployment ID string if parseable from CLI output, otherwise None.

    Raises:
        RuntimeError: If the railway CLI exits with a non-zero code.
    """
    print("Uploading source code...")
    # Personal API tokens are rejected by railway CLI; create a project token
    # scoped to this project/environment for CLI auth.
    # NOTE: project_token is a secret - never log it or pass as a CLI argument.
    project_token = create_project_token(project_id, environment_id, token)

    # The CLI gets the project token and nothing else of value: the account
    # token (RAILWAY_API_TOKEN), the Anthropic and mail keys and the worker's
    # database URL stay out of its environment. It authenticates with
    # RAILWAY_TOKEN alone, which also fixes the project and environment.
    cli_env = env_without_secrets()
    cli_env["RAILWAY_TOKEN"] = project_token

    # List form avoids shell injection; RAILWAY_TOKEN is set in env, not argv.
    result = subprocess.run(
        ["railway", "up", "--detach", "--service", service_id],
        cwd=build_dir,
        capture_output=True,
        text=True,
        timeout=120,  # prevent indefinite hang
        env=cli_env,
    )
    cli_secrets = {project_token, token}
    if result.returncode != 0:
        raise RuntimeError(
            f"railway up failed (exit {result.returncode}):\n"
            + _scrub(f"{result.stderr.strip()}\n{result.stdout.strip()}", cli_secrets, limit=4000)
        )
    output = (result.stdout + result.stderr).strip()
    print(f"  {_scrub(output, cli_secrets, limit=4000)}")

    # v2-deploy-source-deployment-id-fix (2026-05-02): the railway CLI's
    # "Build Logs" URL has the form
    # https://railway.com/project/<project_id>/service/<service_id>?id=<deployment_id>
    # Prior regex matched a path-segment UUID first, which captured the
    # service_id (the path's last UUID before the query string) instead
    # of the deployment_id. The deployment_id lives ONLY in the query
    # string `?id=<uuid>`. Try query-string first; fall through to legacy
    # patterns only if none found.
    UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"

    # Pattern 1 (preferred): `?id=<uuid>` or `&id=<uuid>` query-string param,
    # which is the canonical deployment_id per railway CLI.
    qs_match = re.search(rf"[?&]id=({UUID})", output)
    if qs_match:
        return qs_match.group(1)

    # Pattern 2 (legacy fallback): URL path segment UUID. NOTE: in current
    # railway CLI output this captures service_id, not deployment_id; kept
    # for back-compat with older CLI versions that emitted a different URL
    # shape. wait_for_deployment will fail on this id; the caller falls
    # through to get_latest_deployment() to recover.
    uuid_in_url = re.search(rf"https?://[^\s]+/({UUID})", output)
    if uuid_in_url:
        return uuid_in_url.group(1)

    # Pattern 3 (last-ditch): standalone UUID anywhere in the output.
    uuid_match = re.search(UUID, output)
    if uuid_match:
        return uuid_match.group(0)
    return None


def get_latest_deployment(service_id, environment_id, token):
    """Query Railway for the most recent deployment on this service.

    Args:
        service_id: Railway service ID.
        environment_id: Railway environment ID.
        token: Railway API token.

    Returns:
        Deployment ID string, or None if no deployments exist.
    """
    data = gql("""
        query Deployments($serviceId: String!, $environmentId: String!) {
            deployments(first: 1, input: {serviceId: $serviceId, environmentId: $environmentId}) {
                edges { node { id status } }
            }
        }
    """, {"serviceId": service_id, "environmentId": environment_id}, token)
    edges = data["deployments"]["edges"]
    if not edges:
        return None
    return edges[0]["node"]["id"]


class DeploymentLogsUnreadable(RuntimeError):
    """The log of a deployment could not be read (get_deployment_logs with raise_on_error=True)."""


def get_deployment_logs(deployment_id, token, limit=20, *, raise_on_error=False):
    """Fetch the last N log lines for a deployment; returns list of messages.

    By default failures are swallowed and give an empty list, because log
    retrieval is best-effort diagnostic output for most callers. That makes an
    unreadable log look the same as an empty one. A caller that must tell them
    apart (the worker, when it decides whether a crash is the app's fault)
    passes raise_on_error=True and gets DeploymentLogsUnreadable instead; the
    exception names the kind of failure and nothing from the request.

    Args:
        deployment_id: Railway deployment ID.
        token: Railway API token.
        limit: Max log lines to request (Railway caps around a few hundred).
            Defaults to 20 for existing monitor/diagnose callers; worker_pg
            bumps to 100 when persisting to build_logs for crash debugging.
        raise_on_error: Raise DeploymentLogsUnreadable instead of returning [].

    Returns:
        List of log message strings (empty on error unless raise_on_error).
    """
    try:
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            limit_int = 20
        if limit_int < 1:
            limit_int = 1
        data = gql("""
            query Logs($id: String!, $limit: Int!) {
                deploymentLogs(deploymentId: $id, limit: $limit) { message timestamp }
            }
        """, {"id": deployment_id, "limit": limit_int}, token)
        return [entry["message"] for entry in (data.get("deploymentLogs") or [])]
    except Exception as error:
        if raise_on_error:
            raise DeploymentLogsUnreadable(
                f"the log of deployment {deployment_id} could not be read ({type(error).__name__})"
            ) from None
        return []


class DeploymentEnded(RuntimeError):
    """Railway reported a deployment in an end state other than SUCCESS.

    `status` is Railway's own word, seen on two consecutive polls:
      CRASHED  the container was started and its process exited;
      FAILED   Railway did not get as far as a running container (its build
               or its start step failed);
      REMOVED  the deployment was taken away.
    The message keeps the wording callers and logs already know
    ("Deployment crashed after 42s").
    """

    def __init__(self, status: str, elapsed_s: int):
        self.status = status
        self.elapsed_s = elapsed_s
        super().__init__(f"Deployment {status.lower()} after {elapsed_s}s")


def wait_for_deployment(deployment_id, token, timeout=DEPLOY_TIMEOUT,
                        service_id=None, environment_id=None):
    """Poll deployment status until SUCCESS; raise on failure or timeout.

    If the deployment ID turns out to be stale (persistent "not found"), and
    service_id/environment_id are provided, falls back to polling the latest
    deployment on that service.

    Args:
        deployment_id: Railway deployment ID.
        token: Railway API token.
        timeout: Maximum seconds to wait before raising RuntimeError.
        service_id: Optional service ID for fallback deployment lookup.
        environment_id: Optional environment ID for fallback deployment lookup.

    Raises:
        DeploymentEnded: Railway reported CRASHED, FAILED or REMOVED on two
            consecutive polls (a RuntimeError that carries the status).
        RuntimeError: The timeout was exceeded, or the status could not be read.
    """
    print(f"Waiting for deployment to go live (timeout: {timeout}s)...")
    start = time.time()
    not_found_count = 0
    # CRASHED tolerance: Railway frequently reports CRASHED briefly during
    # the first container restart after schema migration, then stabilizes
    # to SUCCESS. Raising on the first CRASHED snapshot short-circuits
    # deploy() with health_check="failed" before the app recovers, which
    # failed builds whose apps came back healthy seconds later. So TWO
    # consecutive CRASHED/FAILED polls are required before raising, giving
    # Railway ~10 seconds to transition to SUCCESS. Any intermediate
    # SUCCESS (within the same loop) resets the counter. FAILED + REMOVED
    # are grouped with CRASHED for the same tolerance window; a genuine
    # crash will persist across the next poll and still raise.
    transient_failure_count = 0
    _MAX_CONSECUTIVE_TRANSIENT_FAILURES = 2
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        try:
            data = gql("""
                query Deployment($id: String!) {
                    deployment(id: $id) { id status }
                }
            """, {"id": deployment_id}, token)
        except RuntimeError as err:
            # "Deployment not found" can be transient during Railway's
            # eventual consistency window; retry before giving up.
            if "not found" in str(err).lower() or "Deployment not found" in str(err):
                not_found_count += 1
                if not_found_count <= 12:
                    print(f"  Deployment {deployment_id} not found yet, retrying ({elapsed}s, attempt {not_found_count}/12)...")
                    time.sleep(10)
                    continue
                # After 12 retries (2 min), try falling back to latest deployment
                if service_id and environment_id:
                    print(f"  Deployment {deployment_id} still not found; trying latest deployment on service...")
                    try:
                        fallback_id = get_latest_deployment(service_id, environment_id, token)
                        if fallback_id and fallback_id != deployment_id:
                            print(f"  Switched to deployment {fallback_id}")
                            deployment_id = fallback_id
                            not_found_count = 0
                            continue
                    except RuntimeError:
                        pass
            raise
        not_found_count = 0
        status = data["deployment"]["status"]
        if status == "SUCCESS":
            print(f"  Deployment succeeded ({elapsed}s).")
            return
        if status in ("FAILED", "CRASHED", "REMOVED"):
            transient_failure_count += 1
            if transient_failure_count < _MAX_CONSECUTIVE_TRANSIENT_FAILURES:
                # First observation of a failure-like status: give Railway
                # one more poll cycle to recover. Do NOT log deployment
                # logs yet: fetching them can be slow and we may not
                # need them.
                print(
                    f"  {status} ({elapsed}s): waiting for next poll to confirm "
                    f"(consecutive transient failures: {transient_failure_count}/{_MAX_CONSECUTIVE_TRANSIENT_FAILURES})..."
                )
                time.sleep(10)
                continue
            logs = get_deployment_logs(deployment_id, token)
            if logs:
                print("  Last deployment logs:")
                for line in logs[-10:]:
                    # The customer app's own output: keep database passwords
                    # out of the worker log.
                    print(f"    {redact_url(str(line))}")
            raise DeploymentEnded(status, elapsed)
        # Any non-terminal, non-success status (BUILDING, DEPLOYING, etc.)
        # resets the transient-failure counter: a flicker followed by
        # progress is the normal CRASHED→SUCCESS transition.
        transient_failure_count = 0
        print(f"  {status} ({elapsed}s)...")
        time.sleep(10)
    raise RuntimeError(f"Deployment timed out after {timeout}s")


# ---------------------------------------------------------------------------
# Post-deploy verification
# ---------------------------------------------------------------------------
#
# The probes below make the worker send requests to a deployed app, and drive a
# browser at it. The base URL comes from a Railway response (or the operator's
# custom domain), the route paths come from the build spec, which is customer
# influenced, and the app itself can answer with redirects or pull in other
# URLs. So: the base URL must pass monitor.ping.probe_url_problem, spec paths
# are joined with _app_url (which refuses anything that would change the host),
# every HTTP probe is sent through monitor.ping.guarded_session (each
# connection resolves the host once, refuses a non-public address and connects
# to the address it checked), HTTP probes are never followed off the app's own
# origin, and the browser aborts requests to non-public destinations
# (_guard_browser_page; a browser's own DNS lookup cannot be pinned, so that
# one is resolve-then-check).

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url):
    """(scheme, host, port) of a URL. Raises ValueError for a malformed one."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


def _is_route_path(path) -> bool:
    """True for a plain "/..." route path with no backslash, space or control character.

    A spec path that does not start with "/" would be glued onto the host name
    when appended to a base URL ("https://app.example" + "@internal:8080/x" is
    a URL for host "internal"), so it is not a route.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    return "\\" not in path and not any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in path)


def _app_url(base, path):
    """`base` + `path` for a route path taken from a spec, or None.

    None when `path` is not a route path (_is_route_path) or when the joined
    URL does not have the origin of `base`.
    """
    if not _is_route_path(path):
        return None
    joined = f"{base.rstrip('/')}{path}"
    try:
        if _origin(joined) != _origin(base):
            return None
    except ValueError:
        return None
    return joined


def _same_origin_session(base):
    """A guarded requests.Session that only talks to the origin of `base`.

    Session.send is the one place every request passes through, redirect hops
    included, so a deployed app cannot bounce a probe (and the cookies of the
    session) to another host by answering with a Location header. Underneath,
    monitor.ping.guarded_session supplies the connection-level guard: the
    host is resolved once per connection and only public addresses are
    connected to. It also bounds every request: the `timeout` given to get()
    or post() is the deadline of the whole exchange, redirects included, and
    at most PAGE_BODY_MAX_BYTES of a page are read.
    """
    session = guarded_session(max_body_bytes=PAGE_BODY_MAX_BYTES)
    allowed = _origin(base)
    real_send = session.send

    def _guarded_send(request, **kwargs):
        try:
            target = _origin(request.url)
        except ValueError:
            target = None
        if target != allowed:
            raise requests.exceptions.InvalidURL(
                f"probe refused to leave {allowed[0]}://{allowed[1]}"
            )
        return real_send(request, **kwargs)

    session.send = _guarded_send
    return session


# Options for every page the worker opens on a deployed app: nothing the page
# offers is saved to the worker's disk, and service workers (whose requests
# bypass request routing) are not registered. The browser process itself is
# launched with env_without_secrets(): it renders customer content, so it gets
# the same scrubbed environment as every other child that handles it.
_BROWSER_PAGE_OPTIONS = {"accept_downloads": False, "service_workers": "block"}

# Bounds on that browser. Playwright's own default for a step with no explicit
# timeout is 30 seconds; every page gets these instead (_bound_browser_page),
# so no click, fill, wait or screenshot waits longer than a step and no
# navigation longer than a navigation. A screenshot is at most this tall,
# whatever the height of the page: a full-page capture of an endless page
# would otherwise be an image of unbounded size.
_BROWSER_STEP_TIMEOUT_MS = 10_000
_BROWSER_NAVIGATION_TIMEOUT_MS = 20_000
_SCREENSHOT_MAX_HEIGHT_PX = 16_000
_BROWSER_VIEWPORT = {"width": 1280, "height": 800}
# Wall-clock budget of capture_deploy_screenshots, checked before each page.
_SCREENSHOT_RUN_BUDGET_S = 90


def _log_browser_wait(what, error):
    """A wait in the browser ended without its condition, most often by its own
    time limit. The step after it runs anyway, so this is logged, not raised."""
    logger.debug("browser wait (%s) ended early: %s: %s", what, type(error).__name__, str(error)[:200])


def _bound_browser_page(page):
    """Give every later step on `page` an explicit time limit."""
    page.set_default_timeout(_BROWSER_STEP_TIMEOUT_MS)
    page.set_default_navigation_timeout(_BROWSER_NAVIGATION_TIMEOUT_MS)


def _full_page_clip():
    """Clip for a full-page screenshot: the page from its top, cut at
    _SCREENSHOT_MAX_HEIGHT_PX (Playwright trims the clip to a shorter page)."""
    return {"x": 0, "y": 0, "width": _BROWSER_VIEWPORT["width"], "height": _SCREENSHOT_MAX_HEIGHT_PX}


def _guard_browser_page(page):
    """Make the browser that renders a deployed app refuse non-public destinations.

    The page is customer content loaded by a browser that runs inside the
    worker's network. Every request made in the page's context (navigation,
    redirect hop, iframe, image, script, fetch) is checked: http(s) to a host
    that passes probe_url_problem and resolves only to public addresses is let
    through, as are data:, blob: and about: URLs, which never leave the
    browser. Everything else is aborted, which covers file: URLs and loopback,
    private, link-local and internal-name destinations.

    Not covered: WebSocket and WebRTC traffic (Playwright does not route it),
    and a DNS answer that changes between this check and the browser's own
    lookup.
    """
    resolution_blocked = {}

    def _blocked(url):
        parts = urlsplit(url)
        if parts.scheme.lower() in ("data", "blob", "about"):
            return False
        if probe_url_problem(url):
            return True
        host = (parts.hostname or "").lower()
        if host not in resolution_blocked:
            resolution_blocked[host] = host_resolution_problem(host) is not None
        return resolution_blocked[host]

    def _handle(route):
        try:
            blocked = _blocked(route.request.url)
        except Exception:
            blocked = True
        if blocked:
            route.abort("blockedbyclient")
        else:
            route.continue_()

    page.context.route("**/*", _handle)


def health_status_word(status_code, body_text):
    """The `status` field of a /health answer, lower-cased, or None.

    A generated app answers /health with a JSON object: {"status": "ok"} with
    200 when its database answers and its home page rendered at boot,
    {"status": "db_error"} or {"status": "home_page_error"} with 503 otherwise
    (compose/build.py; the v1 prompt asks for the same "ok" shape). None
    means the body was not a JSON object with a text `status`. The HTTP status
    is not judged here; see health_response_ok.
    """
    try:
        payload = json.loads(body_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return None
    return payload["status"].strip().lower()


def health_response_ok(status_code, body_text) -> bool:
    """True only for HTTP 200 whose JSON body says status "ok".

    The one definition of a healthy answer: verify_health uses it for a fresh
    deploy and queue/watchdog_probe.py for a deploy it finds afterwards. A 200
    that merely contains the letters "ok" (an error page, a platform
    placeholder, "token": "...") is not healthy.
    """
    return status_code == 200 and health_status_word(status_code, body_text) == "ok"


def verify_health(url, timeout=120):
    """Poll the app's /health endpoint after deploy to confirm it serves HTTP.

    Non-blocking: returns a status string, never raises.

    Args:
        url: The live URL of the deployed app (e.g. https://app.up.railway.app).
        timeout: Maximum seconds to wait for a healthy response. Default 120s
            covers typical Railway edge propagation (30-90s) plus container boot
            (5-20s). With 60s, apps whose container was already listening were
            reported as failed because Railway's edge had not yet routed the
            public URL.

    Returns:
        "ok" only when /health answered 200 with a JSON body whose status is
        "ok" (health_response_ok),
        "failed" for code errors,
        "infra" if the app responds but DB/infra is down (not a code bug):
        its own {"status": "db_error"}, or a body that names a database
        connection problem,
        "infra" if no response ever arrived (URL not yet routable; likely
        Railway edge propagation or container still booting; safer to retry
        via the infra-wait path than to assume code bug).
        "infra" is not a pass: the worker keeps waiting and, if the app stays
        silent, ends the attempt as a transient failure (see _run_pipeline).

    Each poll is one bounded exchange of at most 5 seconds and
    monitor.ping.HEALTH_BODY_MAX_BYTES of body (see guarded_session). An
    answer that is slower or larger than that is not a healthy one: a slow
    one counts as no response, and a body cut at the cap does not parse as
    the {"status": "ok"} object.
    """
    problem = probe_url_problem(url)
    if problem:
        print(f"  Health check refused ({problem}); not probing.")
        return "failed"
    health_url = f"{url.rstrip('/')}/health"
    print(f"Verifying health at {health_url} (timeout: {timeout}s)...")
    deadline = time.time() + timeout
    last_body = ""
    last_word = None
    last_error = None
    got_any_response = False
    session = guarded_session()
    while time.time() < deadline:
        try:
            # No redirects: a healthy /health answers 200 itself. A name that
            # does not resolve yet, or resolves to a non-public address, is a
            # connection error here and counts as "no response".
            resp = session.get(health_url, timeout=5, allow_redirects=False)
            got_any_response = True
            if health_response_ok(resp.status_code, resp.text):
                print("  Health check passed.")
                return "ok"
            last_body = resp.text[:500]
            last_word = health_status_word(resp.status_code, resp.text)
        except Exception as poll_error:
            # No usable answer on this poll (not routable yet, refused, timed
            # out): try again until the deadline. The last error is reported
            # below if no poll ever got an answer.
            last_error = poll_error
        time.sleep(5)

    # Distinguish infra issues from code bugs.
    # If the app process is running (got HTTP responses) but DB is down,
    # that's infrastructure, not a code error Claude can fix.
    _INFRA_SIGNALS = [
        "connection timeout", "connection terminated", "connection refused",
        "too many connections", "database system is starting up",
        "ECONNREFUSED", "ETIMEDOUT", "the database system is shutting down",
    ]
    lower_body = last_body.lower()
    if got_any_response and (last_word == "db_error"
                             or any(sig.lower() in lower_body for sig in _INFRA_SIGNALS)):
        print(f"  Health check failed (infrastructure issue, not code bug): {_scrub(last_body, limit=100)}")
        return "infra"

    # If NO response ever arrived within the window, the URL is likely not
    # yet routable (Railway edge propagation) or the container is still
    # booting. A genuine app crash would have produced 502/503 error pages
    # from Railway's edge (got_any_response=True), so silence here is a
    # specific "not reachable yet" signal. Return "infra" so the caller
    # routes through the existing 3-minute retry-wait loop instead of
    # declaring a code bug and running destructive fix paths.
    if not got_any_response:
        print("  Health check received no responses (URL not yet routable or app still booting); treating as infra/propagation.")
        if last_error is not None:
            print(f"  Last probe error: {type(last_error).__name__}: {_scrub(last_error, limit=200)}")
        return "infra"

    print("  Health check failed (app may still be booting).")
    return "failed"


def verify_auth(url, spec, timeout=30):
    """Test POST /login against the deployed app to catch SSL/DB issues.

    Attempts to login with the seeded admin credentials. This catches issues
    that the /health endpoint misses (e.g. SSL misconfiguration on the
    session store, missing database tables, bcrypt hash mismatches).

    This is a v1 probe: it posts the v1 default seed password. A v2 app seeds
    its admin without a password, so the probe cannot succeed there and the
    worker does not run it for v2 (see deploy()'s password_login_probe).

    When USE_MAGIC_LINK_AUTH is true (the default) the probe still runs, but a
    failed password login is reported as "warning" instead of "failed". Note
    that deploy() treats any result other than "ok" as a failed health check.

    Non-blocking: returns a status string, never raises.

    Args:
        url: The live URL of the deployed app.
        spec: App spec dict (contains admin_email for login).
        timeout: Maximum seconds to wait.

    Returns:
        "ok" if login redirects to dashboard (302),
        "warning" if magic link auth mode and password login fails (non-blocking),
        "failed" otherwise.
    """
    is_magic_link = magic_link_auth_enabled()

    admin_email = spec.get("admin_email", "admin@example.com")
    base = url.rstrip("/")

    # Try both /login and /admin/login (some apps put auth under /admin)
    login_paths = ["/login", "/admin/login"]
    login_url = None

    def _result(status):
        """Downgrade 'failed' to 'warning' in magic link mode (non-blocking)."""
        if status == "failed" and is_magic_link:
            return "warning"
        return status

    problem = probe_url_problem(base)
    if problem:
        print(f"  Auth check refused ({problem}); not probing.")
        return _result("failed")

    print(f"Verifying auth at {base}...")
    deadline = time.time() + timeout
    session = _same_origin_session(base)

    while time.time() < deadline:
        try:
            # Find which login path works
            if login_url is None:
                for path in login_paths:
                    try:
                        resp = session.get(f"{base}{path}", timeout=5)
                        if resp.status_code == 200 and "csrf" in resp.text.lower():
                            login_url = f"{base}{path}"
                            break
                    except Exception as probe_error:
                        logger.debug("login page probe of %s did not answer: %s", path, probe_error)
                if login_url is None:
                    time.sleep(3)
                    continue

            # Get login page + CSRF token
            login_page = session.get(login_url, timeout=5)
            if login_page.status_code != 200:
                time.sleep(3)
                continue

            # Extract CSRF token from form
            csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', login_page.text)
            if not csrf_match:
                print("  Auth check: no CSRF token found on login page.")
                return _result("failed")

            csrf_token = csrf_match.group(1)

            # POST login with seeded admin credentials
            resp = session.post(
                login_url,
                data={"email": admin_email, "password": "password123", "_csrf": csrf_token},
                allow_redirects=False,
                timeout=5,
            )

            if resp.status_code == 302:
                location = resp.headers.get("location", "")
                if "/login" not in location:
                    print(f"  Auth check passed (redirect to {location}).")
                    return "ok"
                else:
                    print("  Auth check: login redirected back to login (bad credentials or session issue).")
                    return _result("failed")
            elif resp.status_code == 500:
                print("  Auth check: login returned 500 (DB/SSL/session error).")
                return _result("failed")
            else:
                print(f"  Auth check: unexpected status {resp.status_code}.")
                return _result("failed")

        except Exception:
            # App might still be booting
            time.sleep(3)

    print("  Auth check timed out.")
    return _result("failed")


def verify_data(url, spec, timeout=15):
    """Verify deployed app has seed data by checking CRUD list page sizes.

    Fetches the first few spec-defined data pages and checks that the response
    body is large enough to contain rendered seed data (not just an empty shell).
    Non-blocking: returns a status dict, never raises.

    Args:
        url: The live URL of the deployed app.
        spec: App spec dict (contains pages for route paths).
        timeout: Per-request timeout in seconds.

    Returns:
        dict with keys: status ("ok"|"warning"), pages_checked, pages_empty, details.
    """
    base = url.rstrip("/")
    result = {"status": "ok", "pages_checked": 0, "pages_empty": 0, "details": []}

    # Collect CRUD list pages from spec
    pages_to_check = []
    for page in spec.get("pages", []):
        path = page.get("path", "") if isinstance(page, dict) else ""
        if not isinstance(path, str):
            continue
        if path and path != "/" and not path.startswith("/login") and not path.startswith("/register"):
            pages_to_check.append(path)
    # Also check model-derived CRUD list paths
    for model in spec.get("data_models", []):
        name = model.get("name", "") if isinstance(model, dict) else ""
        if name and isinstance(name, str):
            # Convert CamelCase to slug
            slug = re.sub(r'(?<!^)(?=[A-Z])', '-', name).lower()
            path = f"/{slug}"
            if path not in pages_to_check:
                pages_to_check.append(path)

    # Spec paths are customer-influenced: keep only those that stay on the app.
    pages_to_check = [path for path in pages_to_check if _app_url(base, path)]
    if not pages_to_check:
        return result

    problem = probe_url_problem(base)
    if problem:
        print(f"  Data check refused ({problem}); not probing.")
        result["status"] = "warning"
        result["details"].append(f"probe refused: {problem}")
        return result

    # Check up to 5 pages
    session = _same_origin_session(base)
    # Try to login first (some pages require auth)
    admin_email = spec.get("admin_email", "admin@example.com")
    try:
        login_resp = session.get(f"{base}/login", timeout=timeout)
        if login_resp.status_code == 200:
            csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', login_resp.text)
            if csrf_match:
                session.post(
                    f"{base}/login",
                    data={"email": admin_email, "password": "password123", "_csrf": csrf_match.group(1)},
                    allow_redirects=True,
                    timeout=timeout,
                )
    except Exception as login_error:
        # Continue without auth: the pages below are then read logged out.
        logger.debug("data check: login did not complete: %s", login_error)

    MIN_CONTENT_SIZE = 2000  # bytes: an empty shell is ~500-1000; seed data pushes it well above
    for path in pages_to_check[:5]:
        try:
            resp = session.get(f"{base}{path}", timeout=timeout, allow_redirects=True)
            result["pages_checked"] += 1
            if resp.status_code == 200 and len(resp.text) < MIN_CONTENT_SIZE:
                result["pages_empty"] += 1
                result["details"].append(f"{path}: {len(resp.text)} bytes (expected >{MIN_CONTENT_SIZE})")
        except Exception as page_error:
            # Not counted in pages_checked: an unread page is neither empty nor full.
            logger.debug("data check: %s was not read: %s", path, page_error)

    if result["pages_empty"] > 0:
        result["status"] = "warning"
        print(f"  Data check warning: {result['pages_empty']}/{result['pages_checked']} pages appear empty")
    elif result["pages_checked"] > 0:
        print(f"  Data check passed ({result['pages_checked']} pages verified)")

    return result


def verify_magic_link_auth(url, raw_token, timeout=30):
    """Verify login by link on the deployed app by using up the worker's own token.

    The worker seeds two tokens (see _inject_magic_link_token in
    queue/worker_pg.py): this one, for verification, and a second one that is
    emailed to the customer. The probe signs in with the verification token
    the way the confirmation page does:

      1. GET /login, to obtain a session cookie and the form's `_csrf` value.
      2. POST /magic/consume with `token` and `_csrf` in the request BODY.

    A redirect to anywhere but /login means the seeded hash matched, the app
    set `used_at` and a session was created: the login flow works end to end.

    The token never appears in a URL, so it cannot land in the app's access
    log, in a proxy log or in a Referer header; and it is single use, so after
    a successful probe it is dead wherever else it may have been copied. The
    customer's token is not touched.

    The contract with the generated app (compose/build.py, routes/auth.js):
    GET /login renders `<input type="hidden" name="_csrf" value="...">`, and
    POST /magic/consume (form encoded `token`, `_csrf`) answers a redirect to
    a path other than /login on success and to /login?error=<code> for an
    unknown, used or expired token. A v1 app that only implements
    GET /magic?token= has no such route and is reported as "failed"; the
    worker treats that as a warning, never as a build failure.

    Args:
        url: The live URL of the deployed app.
        raw_token: The raw hex verification token (not the hash).
        timeout: Maximum seconds to wait.

    Returns:
        "ok" if the token signed in (and is now used up),
        "skipped" if magic link auth is disabled or there is no token,
        "failed" otherwise.
    """
    if not magic_link_auth_enabled():
        return "skipped"
    if not raw_token:
        print("  Magic link check skipped (no token available).")
        return "skipped"

    base = url.rstrip("/")
    problem = probe_url_problem(base)
    if problem:
        print(f"  Magic link check refused ({problem}); not probing.")
        return "failed"
    print(f"  Verifying magic link auth at {base}...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            session = _same_origin_session(base)
            login_page = session.get(f"{base}/login", timeout=10, allow_redirects=False)
            if login_page.status_code != 200:
                # The app may still be starting; try again until the deadline.
                time.sleep(3)
                continue
            csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', login_page.text)
            if not csrf_match:
                print("  Magic link auth failed: no CSRF token on the login page.")
                return "failed"
            resp = session.post(
                f"{base}/magic/consume",
                data={"token": raw_token, "_csrf": csrf_match.group(1)},
                allow_redirects=False,
                timeout=10,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                target = urlsplit(resp.headers.get("location", "")).path
                if target and not target.startswith("/login"):
                    print("  Magic link auth passed (the verification token signed in and is now used up).")
                    return "ok"
                print("  Magic link auth failed: the app refused the verification token.")
                return "failed"
            if resp.status_code in (404, 405):
                print("  Magic link auth failed: the app has no POST /magic/consume route.")
                return "failed"
            print(f"  Magic link auth failed: unexpected status {resp.status_code} from POST /magic/consume.")
            return "failed"
        except Exception:
            time.sleep(3)

    print("  Magic link auth check timed out.")
    return "failed"


def verify_routes(url, spec, timeout=60):
    """Verify all spec-defined routes return 200 (not 500) after deploy.

    Logs in with seeded admin credentials, then visits every page defined in
    the spec. Any route returning 500 indicates a broken deploy that should
    not reach "preview" status.

    This is a lightweight HTTP check (no browser) designed to run in ~10-30s.
    It catches deploy-specific issues that smoke tests (which run pre-deploy
    on localhost) might miss: SSL misconfiguration, missing env vars,
    Railway-specific path issues.

    Args:
        url: The live URL of the deployed app.
        spec: App spec dict (contains pages, admin_email).
        timeout: Maximum seconds for the entire verification.

    Returns:
        Dict with:
            status: "ok" if all routes pass, "failed" if any return 500
            total: Number of routes checked
            passed: Number that returned 200/302
            failing_routes: List of "path -> status_code" strings
    """
    base = url.rstrip("/")
    admin_email = spec.get("admin_email", "admin@example.com")
    pages = spec.get("pages", [])
    deadline = time.time() + timeout

    result = {"status": "ok", "total": 0, "passed": 0, "failing_routes": []}

    problem = probe_url_problem(base)
    if problem:
        print(f"  Route verify refused ({problem}); not probing.")
        result["status"] = "failed"
        result["failing_routes"].append(f"probe refused: {problem}")
        return result

    print(f"Verifying {len(pages)} spec routes at {base}...")

    try:
        session = _same_origin_session(base)

        # Step 1: Log in
        login_resp = session.get(f"{base}/login", timeout=5)
        csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', login_resp.text)
        if not csrf_match:
            print("  Route verify: no CSRF token on login page.")
            result["status"] = "failed"
            result["failing_routes"].append("/login -> no CSRF token")
            return result

        csrf = csrf_match.group(1)
        login_post = session.post(
            f"{base}/login",
            data={"email": admin_email, "password": "password123", "_csrf": csrf},
            allow_redirects=True,
            timeout=10,
        )

        # Step 2: Check all spec pages
        checked_paths = set()
        for page_entry in pages:
            if time.time() > deadline:
                print("  Route verify: timeout reached.")
                break

            page_path = page_entry.get("path", "/") if isinstance(page_entry, dict) else None
            page_url = _app_url(base, page_path)
            if page_url is None:
                # Not a route on this app (see _app_url); never request it.
                print(f"  Route verify: skipping unusable spec path {page_path!r}")
                continue
            # Skip parameterized routes (e.g. /todos/:id)
            if ":id" in page_path or ":slug" in page_path:
                continue
            if page_path in checked_paths:
                continue
            checked_paths.add(page_path)
            result["total"] += 1

            try:
                resp = session.get(page_url, timeout=8, allow_redirects=True)
                if resp.status_code == 500:
                    # Retry once (transient DB race)
                    time.sleep(2)
                    resp = session.get(page_url, timeout=8, allow_redirects=True)

                if resp.status_code == 500:
                    result["failing_routes"].append(f"{page_path} -> 500")
                elif resp.status_code == 404:
                    result["failing_routes"].append(f"{page_path} -> 404")
                else:
                    result["passed"] += 1
            except requests.Timeout:
                # Slow infra, not a code bug -- retry once with longer timeout
                try:
                    time.sleep(3)
                    resp = session.get(page_url, timeout=12, allow_redirects=True)
                    if resp.status_code == 500:
                        result["failing_routes"].append(f"{page_path} -> 500")
                    elif resp.status_code == 404:
                        result["failing_routes"].append(f"{page_path} -> 404")
                    else:
                        result["passed"] += 1
                except Exception as retry_error:
                    # Timeout on retry = infra issue, skip rather than fail the route
                    logger.info("route check: %s did not answer on the retry: %s", page_path, retry_error)
            except Exception as exc:
                result["failing_routes"].append(f"{page_path} -> error: {exc}")

        # Step 3: Also check common routes not in spec
        for common_path in ["/login", "/signup", "/register"]:
            if common_path in checked_paths:
                continue
            result["total"] += 1
            try:
                # A fresh (logged-out) session, still pinned to the app's origin.
                resp = _same_origin_session(base).get(f"{base}{common_path}", timeout=5)
                if resp.status_code == 500:
                    result["failing_routes"].append(f"{common_path} -> 500")
                elif resp.status_code != 404:  # 404 is OK for /signup vs /register
                    result["passed"] += 1
            except Exception as route_error:
                logger.info("route check: %s did not answer: %s", common_path, route_error)

        if result["failing_routes"]:
            result["status"] = "failed"
            print(f"  Route verify FAILED: {len(result['failing_routes'])} routes broken")
            for fr in result["failing_routes"][:5]:
                print(f"    {fr}")
        else:
            print(f"  Route verify passed: {result['passed']}/{result['total']} routes OK")

    except Exception as exc:
        print(f"  Route verify error: {exc}")
        result["status"] = "failed"
        result["failing_routes"].append(f"setup error: {exc}")

    return result


def capture_deploy_screenshots(live_url, spec, build_dir):
    """Capture screenshots of a deployed app for visual verification.

    Best-effort: failures log a warning but never raise. Takes screenshots
    of the login page and up to 3 auth-required pages (logged in as first
    seed user).

    Args:
        live_url: The live URL of the deployed app.
        spec: Parsed spec dict.
        build_dir: Path to the build directory (screenshots saved here).

    Returns:
        List of absolute file paths to captured screenshots.
    """
    from playwright.sync_api import sync_playwright

    captured = []
    problem = probe_url_problem(live_url)
    if problem:
        print(f"  Deploy screenshot capture refused ({problem}); not loading the app.")
        return captured

    screenshots_dir = Path(build_dir) / "screenshots" / "deploy"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    def _slugify(path_str):
        return path_str.strip("/").replace("/", "-") or "root"

    started = time.monotonic()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, env=env_without_secrets())
            page = browser.new_page(viewport=dict(_BROWSER_VIEWPORT), **_BROWSER_PAGE_OPTIONS)
            _guard_browser_page(page)
            _bound_browser_page(page)

            # 1. Login page
            page.goto(f"{live_url.rstrip('/')}/login", timeout=15000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as wait_error:
                _log_browser_wait("login page network idle", wait_error)
            login_path = str(screenshots_dir / "login.png")
            page.screenshot(path=login_path, full_page=False)
            captured.append(login_path)
            print(f"  Deploy screenshot: /login -> deploy/login.png")

            # 2. Log in as first seed user, visit auth pages
            # Skip authenticated screenshots when magic link auth is enabled
            if magic_link_auth_enabled():
                print("  Deploy screenshots: skipping auth pages (magic link auth mode)")
                return captured

            roles = spec.get("user_roles", [])
            first_role = roles[0]["role"] if roles else None
            if first_role:
                email = f"{first_role}@example.com"
                # Read CSRF token from the login form if present
                csrf_input = page.locator("input[name='_csrf']")
                if csrf_input.count() > 0:
                    csrf_value = csrf_input.get_attribute("value")
                    if csrf_value:
                        page.evaluate(
                            "([name, val]) => { const el = document.querySelector('input[name=\"' + name + '\"]'); if(el) el.value = val; }",
                            ["_csrf", csrf_value],
                        )
                page.fill("input[name='email']", email)
                page.fill("input[name='password']", "test_password_for_ci")
                url_before = page.url
                page.click("button[type='submit']")
                try:
                    page.wait_for_url(lambda url: "/login" not in url, timeout=10000)
                except Exception as wait_error:
                    _log_browser_wait("leaving the login page", wait_error)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception as wait_error:
                    _log_browser_wait("network idle after login", wait_error)
                url_after = page.url

                if "/login" in url_after:
                    print(f"  Deploy screenshot warning: login failed (still on {url_after}), skipping auth pages")
                else:
                    auth_pages = [
                        p["path"] for p in spec.get("pages", [])
                        if p.get("auth_required") and first_role in p.get("allowed_roles", [])
                        and _app_url(live_url, p.get("path"))
                    ][:3]

                    for page_path in auth_pages:
                        if time.monotonic() - started > _SCREENSHOT_RUN_BUDGET_S:
                            print("  Deploy screenshots: time budget used up, skipping the remaining pages")
                            break
                        try:
                            response = page.goto(_app_url(live_url, page_path), timeout=15000)
                            try:
                                page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception as wait_error:
                                _log_browser_wait(f"network idle on {page_path}", wait_error)
                            current_url = page.url
                            if "/login" in current_url:
                                print(f"  Deploy screenshot warning: {page_path} redirected to login, skipping")
                                continue
                            if response and response.status == 200:
                                slug = _slugify(page_path)
                                ss_path = str(screenshots_dir / f"{slug}.png")
                                page.screenshot(path=ss_path, full_page=False)
                                captured.append(ss_path)
                                print(f"  Deploy screenshot: {page_path} -> deploy/{slug}.png")
                        except Exception as page_exc:
                            print(f"  Deploy screenshot warning: {page_path}: {page_exc}")

            page.close()
            browser.close()
    except Exception as exc:
        print(f"  Deploy screenshot capture warning: {exc}")

    return captured


def _pick_internal_render_path(spec):
    """Pick one public internal page to render alongside the home page.

    Preference order: first public CRUD list-view page, then /contact, /about,
    then any non-root spec page that is not auth-required and not parameterized.
    Returns a path string, or None if no candidate is found.
    """
    pages = (spec or {}).get("pages") or []
    candidates_public = []
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", "")
        # Spec paths are customer-influenced. Only a plain "/..." route is a
        # page of this app (see _app_url); anything else is not a candidate.
        if not _is_route_path(path) or path == "/":
            continue
        if ":id" in path or ":slug" in path or "{" in path:
            continue
        if (entry or {}).get("auth_required"):
            continue
        candidates_public.append((path, entry or {}))

    # 1. CRUD list views from spec data_models (e.g. /services, /menu).
    data_models = (spec or {}).get("data_models") or []
    list_paths = set()
    for model in data_models:
        public_path = (model or {}).get("public_listing_path")
        if isinstance(public_path, str) and public_path.startswith("/"):
            list_paths.add(public_path)
    for path, _entry in candidates_public:
        if path in list_paths:
            return path

    # 2. /contact, /about: overwhelmingly common public pages.
    by_path = {p: e for p, e in candidates_public}
    for preferred in ("/contact", "/about"):
        if preferred in by_path:
            return preferred

    # 3. First public spec page.
    if candidates_public:
        return candidates_public[0][0]

    # 4. Conservative fallbacks: try /contact even if not in spec; the worker
    # will treat a 404 as a warning, not a failure.
    return "/contact"


def verify_render(live_url, spec, build_dir, timeout=60):
    """Render the homepage and one internal page in a real browser, capture
    full-page screenshots, and gate the build "verified" state on the result.

    Companion to verify_routes / verify_auth / verify_data: those are HTTP
    checks that miss visual defects (cream-on-cream hero, broken layout,
    hardcoded contact info). This step actually loads the deployed app in
    Chromium and saves what the customer would see.

    Failures (timeout, navigation error, non-200) DO NOT fail the build;
    they are downgraded to a warning. The worker's reaction to "warning"
    is to log + continue (the build still reaches preview), preserving the
    behavior of every other verify_* helper.

    Args:
        live_url: The live URL of the deployed app.
        spec: Parsed spec dict (used to pick the internal page).
        build_dir: Path to the build directory (screenshots saved here).
        timeout: Budget in seconds for the whole step, checked before each
            page. A page that has started is bounded by its own step and
            navigation timeouts (_bound_browser_page), so the step can run
            past the budget by one page. Each screenshot is cut at
            _SCREENSHOT_MAX_HEIGHT_PX. The browser's memory is not capped.

    Returns:
        Dict with:
            status: "ok" if both pages rendered with HTTP 200, else "warning"
            screenshots: list of absolute screenshot file paths
            pages: list of {path, status_code} dicts (one per attempted page)
            details: short string explaining a non-ok status, or ""
    """
    from playwright.sync_api import sync_playwright

    base = live_url.rstrip("/")
    result = {"status": "ok", "screenshots": [], "pages": [], "details": ""}
    problem = probe_url_problem(base)
    if problem:
        print(f"  Render verify refused ({problem}); not loading the app.")
        result["status"] = "warning"
        result["details"] = f"render refused: {problem}"
        return result

    screenshots_dir = Path(build_dir) / "screenshots" / "deploy"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    internal_path = _pick_internal_render_path(spec)
    targets = [("/", "home")]
    if internal_path and internal_path != "/" and _app_url(base, internal_path):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", internal_path.strip("/")).strip("-") or "internal"
        targets.append((internal_path, f"internal-{slug[:80]}"))

    deadline = time.time() + max(10, int(timeout))

    print(f"Rendering {len(targets)} page(s) for visual verification at {base}...")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, env=env_without_secrets())
            try:
                page = browser.new_page(viewport=dict(_BROWSER_VIEWPORT), **_BROWSER_PAGE_OPTIONS)
                _guard_browser_page(page)
                _bound_browser_page(page)
                for path, label in targets:
                    if time.time() > deadline:
                        result["status"] = "warning"
                        result["details"] = "verify_render timeout reached"
                        print("  Render verify: timeout reached.")
                        break
                    page_target = f"{base}{path}"
                    status_code = None
                    try:
                        response = page.goto(page_target, timeout=20000, wait_until="load")
                        status_code = response.status if response else None
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception as wait_error:
                            _log_browser_wait("network idle before the screenshot", wait_error)
                        ss_path = str(screenshots_dir / f"render-{label}.png")
                        page.screenshot(path=ss_path, full_page=True, clip=_full_page_clip())
                        result["screenshots"].append(ss_path)
                        result["pages"].append({"path": path, "status_code": status_code})
                        if status_code is None or status_code >= 400:
                            result["status"] = "warning"
                            if not result["details"]:
                                result["details"] = f"{path} returned {status_code}"
                            print(f"  Render verify warning: {path} -> {status_code}")
                        else:
                            print(f"  Render verify: {path} -> {status_code} ({ss_path})")
                    except Exception as page_exc:
                        result["status"] = "warning"
                        if not result["details"]:
                            result["details"] = f"{path}: {page_exc}"
                        result["pages"].append({"path": path, "status_code": status_code})
                        print(f"  Render verify warning: {path}: {page_exc}")
                page.close()
            finally:
                browser.close()
    except Exception as exc:
        result["status"] = "warning"
        if not result["details"]:
            result["details"] = f"render setup error: {exc}"
        print(f"  Render verify warning: {exc}")

    return result


def list_projects(token, deadline=None):
    """List all Railway projects for the authenticated user.

    `deadline` is an optional time.monotonic() value passed on to gql().

    Returns:
        List of dicts with keys: id, name, createdAt.
    """
    data = gql("""
        query {
            projects {
                edges {
                    node {
                        id
                        name
                        createdAt
                    }
                }
            }
        }
    """, token=token, deadline=deadline)
    return [edge["node"] for edge in data["projects"]["edges"]]


def get_service_domain(project_id, token, deadline=None):
    """Return the first public domain registered on any service in a project.

    Used by the supervisor's zombie-thread reality check
    (queue.worker_pg._supervisor_reality_probe): when a build is force-failed
    because its thread got C-blocked, the deploy may have actually completed
    post-kill. Probing `https://{domain}/health` is the only authoritative
    signal the CAS DB can't give us.

    Pure read: no side effects, no mutations.

    Args:
        project_id: Railway project ID.
        token: Railway API token.
        deadline: Optional time.monotonic() value passed on to gql().

    Returns:
        dict with keys `{"domain": str, "service_id": str}` on first hit,
        or None if the project has no services, no service domains, or the
        GraphQL call fails.
    """
    query = """
        query($id: String!) {
            project(id: $id) {
                services {
                    edges {
                        node {
                            id
                            serviceInstances {
                                edges {
                                    node {
                                        domains {
                                            serviceDomains {
                                                domain
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    """
    try:
        data = gql(query, variables={"id": project_id}, token=token, deadline=deadline)
    except Exception as lookup_error:
        print(f"  Could not read the service domain of project {project_id}: "
              f"{type(lookup_error).__name__}")
        return None
    project = (data or {}).get("project") or {}
    services = (project.get("services") or {}).get("edges") or []
    for service_edge in services:
        service_node = (service_edge or {}).get("node") or {}
        service_id = service_node.get("id")
        instance_edges = (service_node.get("serviceInstances") or {}).get("edges") or []
        for inst_edge in instance_edges:
            inst_node = (inst_edge or {}).get("node") or {}
            domains = (inst_node.get("domains") or {}).get("serviceDomains") or []
            for entry in domains:
                domain = (entry or {}).get("domain")
                if domain:
                    return {"domain": domain, "service_id": service_id}
    return None


def destroy_project(project_id, token):
    """Delete a Railway project. Returns True only when Railway confirmed the delete.

    Every other outcome raises RuntimeError: a protected or unverifiable
    project ("destroy_project BLOCKED: ..."), and a delete that Railway
    rejected, did not confirm or could not be reached for
    ("destroy_project FAILED: ..."). A caller that gets no exception may
    therefore say "deleted" and drop its own record of the project; a caller
    that gets one must not.

    SAFETY: Refuses to delete the CAS platform project or any project whose
    name matches a protected pattern. This is the choke point for every
    Railway project deletion the Python side makes (the worker and
    cli/railway_cleanup.py). The web app's admin delete has its own guarded
    call in web/src/services/railway.ts.

    Protected by default:
      - Any project whose name starts with "craftandship" (the CAS platform)
      - The project ID in RAILWAY_PROJECT_ID env var
      - Any name listed in PROTECTED_RAILWAY_PROJECT_NAMES (comma-separated)

    Args:
        project_id: Railway project ID to delete.
        token: Railway API token.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # ── ID-based hard stop ──────────────────────────────────────────────────
    cas_project_id = os.environ.get("RAILWAY_PROJECT_ID", "")
    if cas_project_id and project_id == cas_project_id:
        raise RuntimeError(
            f"destroy_project BLOCKED: {project_id} is the CAS platform project "
            f"(RAILWAY_PROJECT_ID). Never delete this automatically."
        )

    # ── Name-based hard stop ────────────────────────────────────────────────
    # Look up the project name before doing anything destructive.
    protected_names = {"craftandship-web", "craftandship-worker", "craftandship-staging", "craftandship-recovery"}
    extra = os.environ.get("PROTECTED_RAILWAY_PROJECT_NAMES", "")
    if extra:
        protected_names.update(n.strip().lower() for n in extra.split(",") if n.strip())

    # The name check fails CLOSED: if the name cannot be read (API error, an
    # unexpected response shape, a project that no longer exists) nothing is
    # deleted. An undeleted customer project costs a little money; a deleted
    # platform project is an outage.
    try:
        result = gql(
            "query($id: String!) { project(id: $id) { id name } }",
            {"id": project_id}, token,
        )
        project_name = (result.get("project") or {}).get("name", "")
    except Exception as lookup_error:
        raise RuntimeError(
            f"destroy_project BLOCKED: could not read the name of project {project_id} "
            f"({type(lookup_error).__name__}); refusing to delete a project that was not "
            f"checked against the protected names."
        ) from None
    if not isinstance(project_name, str) or not project_name:
        raise RuntimeError(
            f"destroy_project BLOCKED: project {project_id} has no readable name; "
            f"refusing to delete a project that was not checked against the protected names."
        )

    name_lower = project_name.lower()
    # Block anything whose name starts with "craftandship" or is in the explicit set
    if name_lower.startswith("craftandship") or name_lower in protected_names:
        raise RuntimeError(
            f"destroy_project BLOCKED: project '{project_name}' ({project_id}) "
            f"matches protected name pattern. Add it to customer builds DB or "
            f"delete manually via Railway dashboard."
        )
    _log.info("destroy_project: confirmed project name=%r before deletion", project_name)

    try:
        deleted = gql("""
            mutation ProjectDelete($id: String!) { projectDelete(id: $id) }
        """, {"id": project_id}, token)
    except Exception as error:
        raise RuntimeError(
            f"destroy_project FAILED: project {project_id} ({project_name}) was not deleted: "
            f"{_scrub(error, [token] if token else [])}"
        ) from None
    if not isinstance(deleted, dict) or deleted.get("projectDelete") is not True:
        raise RuntimeError(
            f"destroy_project FAILED: Railway did not confirm the delete of project "
            f"{project_id} ({project_name})."
        )
    print(f"  Cleaned up project {project_id} ({project_name}).")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def read_app_name(build_dir):
    """Read app name from package.json; fall back to directory name.

    Args:
        build_dir: Path to the build directory.

    Returns:
        Raw (unsanitized) app name string.
    """
    fallback = Path(build_dir).name
    package_path = Path(build_dir) / "package.json"
    if package_path.exists():
        try:
            with open(package_path) as file:
                name = json.load(file).get("name", fallback)
        except (OSError, ValueError, AttributeError):
            return fallback
        # package.json is generated content: only a non-empty string is a name.
        return name if isinstance(name, str) and name.strip() else fallback
    return fallback


_PLAIN_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")
_MAIL_DOMAIN_RE = re.compile(r"(?=.{4,253}\Z)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}")


def _generated_app_site_env(build_dir) -> dict:
    """ADMIN_EMAIL and SITE_NAME for a generated app, from the spec it was built from.

    The app's own mailer (services/notify.js, emitted by compose/build.py)
    sends "new submission" notices to ADMIN_EMAIL and signs its mail with
    SITE_NAME. Both are facts about the customer's site, not platform
    secrets: ADMIN_EMAIL is the site owner's address, the same one the build
    seeded as the site's admin user (spec.json admin_email), and SITE_NAME is
    the site's display name (spec.json settings.site_name). A value that is
    missing or not plain text is left out, and the app then uses its own
    default (no notification; "Your site").
    """
    try:
        spec = json.loads((Path(build_dir) / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(spec, dict):
        return {}
    found = {}
    admin_email = spec.get("admin_email")
    if isinstance(admin_email, str) and _PLAIN_EMAIL_RE.fullmatch(admin_email.strip()):
        found["ADMIN_EMAIL"] = admin_email.strip()
    settings = spec.get("settings")
    site_name = settings.get("site_name") if isinstance(settings, dict) else None
    if isinstance(site_name, str):
        site_name = " ".join(site_name.split())
        if site_name and len(site_name) <= 100 and site_name.isprintable() \
                and not any(ch in site_name for ch in "<>\"\\"):
            found["SITE_NAME"] = site_name
    return found


def _generated_app_mail_env() -> dict:
    """Mail settings for a generated customer app: a dedicated key or nothing.

    The platform's own RESEND_API_KEY is never handed to a customer app. Every
    generated app is a separate Railway project running customer-shaped code;
    an environment disclosure or code execution bug in any one of them would
    otherwise expose a key that can send mail as the platform. An operator who
    wants generated apps to send mail sets GENERATED_APP_RESEND_API_KEY to a
    separate, send-only Resend key (ideally restricted to its own sending
    domain). When it is unset the app gets no key: it logs a requested login
    link to its own server log and skips form notifications.

    Setting GENERATED_APP_RESEND_API_KEY to the same value as the platform's
    RESEND_API_KEY would hand the platform key over after all, so that is
    refused: the app is deployed without a mail key, exactly as if the
    variable were unset. The comparison is on the values in this process's
    environment; it cannot tell whether two different keys belong to the same
    Resend account.
    """
    app_key = (os.environ.get(GENERATED_APP_MAIL_KEY_ENV) or "").strip()
    if not app_key:
        print(f"  {GENERATED_APP_MAIL_KEY_ENV} is not set: the app is deployed without a mail key "
              "(login links are written to its server log).")
        return {}
    if app_key == (os.environ.get("RESEND_API_KEY") or "").strip():
        print(f"  Refused: {GENERATED_APP_MAIL_KEY_ENV} is the same key as the platform's RESEND_API_KEY. "
              "The app is deployed without a mail key (login links are written to its server log). "
              "Set a separate send-only key to let generated apps send mail.")
        return {}
    mail_env = {"RESEND_API_KEY": app_key}
    # The app builds its From address from RESEND_FROM_DOMAIN. Without it the
    # emitted code falls back to the mail provider's shared onboarding sender.
    from_domain = (os.environ.get(GENERATED_APP_MAIL_DOMAIN_ENV) or "").strip()
    if from_domain and _MAIL_DOMAIN_RE.fullmatch(from_domain):
        mail_env["RESEND_FROM_DOMAIN"] = from_domain.lower()
    elif from_domain:
        print(f"  Ignored: {GENERATED_APP_MAIL_DOMAIN_ENV} is not a plain domain name.")
    else:
        print(f"  {GENERATED_APP_MAIL_DOMAIN_ENV} is not set: the app sends from the mail "
              "provider's shared onboarding address.")
    return mail_env


def deploy(build_dir, custom_domain=None, *, password_login_probe=True):
    """Run the full Railway deployment pipeline; return deployment details.

    Security measures applied here:
    - app_name is sanitized before any API call.
    - DATABASE_URL is logged only in redacted form.
    - project_token is never logged.
    - All subprocess calls use list form (no shell=True).
    - custom_domain is validated before the API call.

    Args:
        build_dir: Path to the build directory containing the Node.js app.
        custom_domain: Optional customer-owned domain to attach (e.g. 'app.mybusiness.example').
            If provided, Railway returns DNS instructions to point the domain at the service.
            The .up.railway.app URL is always created regardless.
        password_login_probe: When True (the default, used for v1 apps), a
            passing /health is followed by verify_auth(), which posts the v1
            seed password to the app's /login, and anything but a successful
            login turns health_check into "failed". The worker passes False
            for v2 apps: they seed the admin without a password, so that probe
            cannot succeed against them, and health_check is then the result
            of verify_health() alone.

    Returns:
        A dict with keys: live_url, railway_project_id, railway_service_id,
        railway_environment_id, database_url, health_check ("ok", "failed"
        or "infra") and deployment_status, Railway's own end state for the
        deployment: "SUCCESS", "CRASHED" (the container started and its
        process exited) or "FAILED" (Railway's build or start step did not
        complete). With CRASHED or FAILED the project is kept and returned,
        health_check is "failed" and /health was not probed. The live_url is
        the custom domain URL if one was provided and attached successfully;
        otherwise the Railway-managed URL.

    Raises:
        ValueError: If build_dir does not exist or app_name sanitizes to empty.
        RuntimeError: On Railway API or deployment failures.
    """
    build_dir = str(Path(build_dir).resolve())
    if not Path(build_dir).is_dir():
        raise ValueError(f"Build directory not found: {build_dir}")

    check_railway_cli()
    token = get_token()

    # Sanitize app_name before any API use - prevents unexpected chars from
    # influencing Railway project/service names.
    raw_app_name = read_app_name(build_dir)
    app_name = sanitize_app_name(raw_app_name)
    if app_name != raw_app_name:
        print(f"  Note: app_name sanitized from '{raw_app_name}' to '{app_name}'")

    project_id = None

    try:
        project_id = create_project(app_name, token)
        environment_id = get_environment_id(project_id, token)
        service_id = create_service(project_id, app_name, token)
        _db_service_id, database_url = provision_postgres(project_id, environment_id, token)

        # Log only the redacted URL so the Postgres password never appears in logs.
        print(f"  DATABASE_URL (redacted): {redact_url(database_url)}")

        env_vars = {
            "NODE_ENV": "production",
            "SESSION_SECRET": secrets.token_hex(32),
            "PORT": "3000",
            "DATABASE_URL": database_url,
        }
        env_vars.update(_generated_app_site_env(build_dir))
        env_vars.update(_generated_app_mail_env())
        set_env_vars(project_id, environment_id, service_id, env_vars, token)

        # Deploy source first: Railway requires an active deployment before
        # it will accept a domain creation request.
        deployment_id = deploy_source(build_dir, project_id, environment_id, service_id, token)
        if not deployment_id:
            # Retry fetching the deployment ID that the CLI triggered.
            # Railway's eventual consistency can take 30-60s to register new
            # deployments, so we retry aggressively.
            for attempt in range(12):
                try:
                    deployment_id = get_latest_deployment(service_id, environment_id, token)
                except RuntimeError:
                    pass
                if deployment_id:
                    break
                wait = 5 if attempt < 6 else 10
                print(f"  Waiting for deployment to register ({attempt + 1}/12)...")
                time.sleep(wait)
        if not deployment_id:
            raise RuntimeError("Could not determine deployment ID after upload")

        # What Railway itself says about the deployment. CRASHED and FAILED
        # are reported to the caller with the project kept, so the worker can
        # read the deployment's logs (and, on v1, repair and redeploy). Which
        # of the two it was is decided by the status Railway returned, not by
        # words in an error text: an API error that merely mentions "failed"
        # is a failed deploy() like any other, and its project is removed.
        deployment_status = "SUCCESS"
        try:
            wait_for_deployment(deployment_id, token,
                                service_id=service_id, environment_id=environment_id)
        except DeploymentEnded as ended:
            if ended.status not in ("CRASHED", "FAILED"):
                raise
            print(f"\n  {ended}")
            print("  Keeping the project so the worker can read its logs.")
            deployment_status = ended.status
        deployment_crashed = deployment_status != "SUCCESS"

        # Generate Railway-managed domain after the service has an active deployment.
        # Even crashed deployments can get a domain (Railway keeps the service).
        domain = generate_domain(environment_id, service_id, token)
        live_url = f"https://{domain}"

        # Optionally attach a customer-owned custom domain.
        if custom_domain:
            result = add_custom_domain(environment_id, service_id, custom_domain, token)
            if result:
                live_url = f"https://{custom_domain}"

        if deployment_crashed:
            print(f"\nRailway reported the deployment {deployment_status}. URL: {live_url}")
            return {
                "live_url": live_url,
                "railway_project_id": project_id,
                "railway_service_id": service_id,
                "railway_environment_id": environment_id,
                "database_url": database_url,
                "health_check": "failed",
                "deployment_status": deployment_status,
            }

        print(f"\nLive at: {live_url}")
        health_status = verify_health(live_url)

        # If health passes, also verify POST /login works (catches SSL/DB
        # issues). Only for apps that seed an admin password; see the
        # password_login_probe argument.
        if health_status == "ok" and password_login_probe:
            try:
                spec_file = Path(build_dir) / "spec.json"
                app_spec = json.loads(spec_file.read_text()) if spec_file.exists() else {}
            except Exception:
                app_spec = {}
            auth_status = verify_auth(live_url, app_spec)
            if auth_status != "ok":
                print("  Warning: health OK but auth failed (POST /login returns error)")
                health_status = "failed"

        return {
            "live_url": live_url,
            "railway_project_id": project_id,
            "railway_service_id": service_id,
            "railway_environment_id": environment_id,
            "database_url": database_url,
            "health_check": health_status,
            "deployment_status": deployment_status,
        }

    except Exception as error:
        # Log the error message only - never log the token or raw DATABASE_URL.
        print(f"\nDeployment failed: {error}")
        if project_id:
            print("Cleaning up partial deployment...")
            try:
                destroy_project(project_id, token)
            except Exception as cleanup_error:
                # destroy_project raises when it refuses a project and when the
                # delete did not go through. Report that, but keep the original
                # failure as the exception the caller sees.
                print(f"  Warning: project {project_id} was NOT cleaned up: {cleanup_error}")
        raise


def redeploy(build_dir, project_id, environment_id, service_id, live_url):
    """Re-upload source and redeploy to an existing Railway service.

    Unlike deploy(), this does NOT create a new project, service, database,
    or domain. It just uploads the (fixed) source code, waits for the
    deployment, and checks health.

    Args:
        build_dir: Path to the build directory.
        project_id: Existing Railway project ID.
        environment_id: Existing Railway environment ID.
        service_id: Existing Railway service ID.
        live_url: The app's live URL for health checking.

    Returns:
        Dict with keys: success, deployment_id, health_check, deploy_logs.
    """
    build_dir = str(Path(build_dir).resolve())
    if not Path(build_dir).is_dir():
        raise ValueError(f"Build directory not found: {build_dir}")

    check_railway_cli()
    token = get_token()

    print(f"Redeploying to existing service {service_id}...")

    deployment_id = None
    try:
        deployment_id = deploy_source(build_dir, project_id, environment_id, service_id, token)
        if not deployment_id:
            for attempt in range(12):
                try:
                    deployment_id = get_latest_deployment(service_id, environment_id, token)
                except RuntimeError:
                    pass
                if deployment_id:
                    break
                wait = 5 if attempt < 6 else 10
                print(f"  Waiting for deployment to register ({attempt + 1}/12)...")
                time.sleep(wait)
        if not deployment_id:
            return {
                "success": False,
                "deployment_id": None,
                "health_check": "failed",
                "deploy_logs": ["Could not determine deployment ID after upload"],
            }

        wait_for_deployment(deployment_id, token,
                            service_id=service_id, environment_id=environment_id)

        health_status = verify_health(live_url)
        deploy_logs = get_deployment_logs(deployment_id, token)

        return {
            "success": health_status == "ok",
            "deployment_id": deployment_id,
            "health_check": health_status,
            "deploy_logs": deploy_logs,
        }

    except RuntimeError as error:
        # Deployment itself failed (CRASHED, FAILED, etc.)
        deploy_logs = []
        if deployment_id:
            deploy_logs = get_deployment_logs(deployment_id, token)
        return {
            "success": False,
            "deployment_id": deployment_id if deployment_id else None,
            "health_check": "failed",
            "deploy_logs": deploy_logs + [str(error)],
        }


def main():
    """CLI entry point: deploy a built app to Railway.

    Usage:
        python cli/deploy.py <build_dir> [custom_domain]

    Examples:
        python cli/deploy.py builds/landscaping-task-tracker/
        python cli/deploy.py builds/landscaping-task-tracker/ app.landscaper.example
    """
    if len(sys.argv) not in (2, 3):
        print("Usage: python cli/deploy.py <build_dir> [custom_domain]")
        print("Example: python cli/deploy.py builds/landscaping-task-tracker/")
        print("         python cli/deploy.py builds/landscaping-task-tracker/ app.landscaper.example")
        sys.exit(1)

    custom_domain = sys.argv[2] if len(sys.argv) == 3 else None
    try:
        result = deploy(sys.argv[1], custom_domain=custom_domain)
        # database_url carries the generated Postgres password; print it redacted.
        printable = dict(result)
        if printable.get("database_url"):
            printable["database_url"] = redact_url(str(printable["database_url"]))
        print(f"Deploy result: {printable}")
    except Exception as error:
        print(f"Error: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
