"""Guard against text that ties sample data to a customer.

This test reads every text file the repository publishes (code, tests,
documents, hooks and workflow files) and fails on a tag that labels a sample
as a customer's: the words "real" and "customer" joined by a space, a hyphen
or an underscore and followed by a colon. The needle is assembled at runtime
so that this file does not match itself.

It also holds the v2 sample fixtures (`compose/fixtures/*.json`) to the
conventions that keep them placeless: mail and link hosts are reserved
example names, phone numbers are in the fictional 555-01xx range, a state is
the placeholder "ST", and postal codes are a plain run counted up from 00001.

The Medicine Waves data files (`tests/fixtures/*.json` and
`tests/golden/*/spec.json`) get three of those checks: a street address must not
carry a postal code outside the placeholder run, a phone number must be in the
555-01xx block, and an event start must be a sample date in 2030. The failure
knowledge base (`cli/failure_kb.json`) must name no calendar day with a clock
time, the shape a dated event start takes in a signature.

Every check here is structural: the file does not look for names, and it
holds no customer's or private person's name in any form.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "compose" / "fixtures"

# Directories that are never published, matched by name at any depth:
# version control and the directories .gitignore lists (installed
# dependencies, build and test output, generated apps, local tooling state).
SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", "dist", "coverage",
    "test-results", "playwright-report", "builds", "logs", "qa_results",
    ".venv", "venv", ".worktrees", "worktrees", ".idea", ".vscode",
}
# Everything else is read as UTF-8 text; a file that does not decode is
# skipped. These suffixes are skipped without being opened.
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".zip", ".gz", ".pyc",
}

MARKERS = {
    "real-customer tag": re.compile("real" + "[_ -]customer" + r"\s*:", re.I),
}


def _is_local_env_file(filename: str) -> bool:
    """A local environment file (.env, .env.local): ignored by git, never
    published, and not this test's business to open. `.env.example` is
    published and is read."""
    return filename == ".env" or (
        filename.startswith(".env.") and filename != ".env.example"
    )


def _iter_text_files(root: Path):
    """Yield (relative path, text) for every readable text file under root."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
                continue
            if _is_local_env_file(filename):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            yield path.relative_to(root), text


def find_markers(root: Path) -> list[str]:
    """Return 'relative/path:line: kind' for every marker under root."""
    hits: list[str] = []
    for rel, text in _iter_text_files(root):
        for lineno, line in enumerate(text.split("\n"), start=1):
            for kind, pattern in MARKERS.items():
                if pattern.search(line):
                    hits.append(f"{rel.as_posix()}:{lineno}: {kind}")
    return hits


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")


def _reported(root: Path) -> set[tuple[str, str]]:
    """(relative path, kind) pairs the scanner reports under root."""
    return {
        (hit.split(":", 1)[0], hit.rsplit(": ", 1)[1]) for hit in find_markers(root)
    }


def test_repository_has_no_customer_markers():
    hits = find_markers(REPO_ROOT)
    assert not hits, "customer markers found:\n" + "\n".join(hits[:40])


def test_scanner_reports_each_marker_kind(tmp_path):
    """The scanner itself must see every kind, in every spelling, or the
    guard above proves nothing. Each sample line is assembled here, never
    written literally."""
    samples = {
        "real-customer tag": [
            "# Driver: real" + "_customer:sample_name (a date).",
            "<!-- real" + "-customer: sample -->",
            "note = 'Real" + " Customer : sample'",
        ],
    }
    assert set(samples) == set(MARKERS)
    expected = set()
    for kind_index, (kind, lines) in enumerate(samples.items()):
        for line_index, line in enumerate(lines):
            rel = f"compose/sample_{kind_index}_{line_index}.py"
            _write(tmp_path, rel, line)
            expected.add((rel, kind))
    missing = sorted(expected - _reported(tmp_path))
    assert not missing, f"markers the scanner did not report: {missing}"


def test_scanner_reads_every_published_location(tmp_path):
    """Documents, root files, hook and workflow directories and templates
    are published with the code, so a marker in any of them is reported.
    Dependency, build-output and version-control directories are not read."""
    tag = "sample: real" + "_customer: someone"
    published = [
        "README.md",
        "Dockerfile.worker",
        "docs/internals/notes.md",
        ".github/workflows/test.yml",
        ".githooks/pre-commit",
        ".claude/commands/status.md",
        "cli/templates/page.ejs.tmpl",
        "web/views/about.ejs",
        "web/src/db/schema.sql",
        "web/.env.example",
        "tests/fixtures/sample.json",
    ]
    unpublished = [
        ".git/COMMIT_EDITMSG",
        "web/node_modules/pkg/index.js",
        "web/dist/server.js",
        "web/.env",
        "web/.env.local",
        "builds/sample/server.js",
    ]
    for rel in published + unpublished:
        _write(tmp_path, rel, tag)
    flagged = {rel for rel, _kind in _reported(tmp_path)}
    assert flagged == set(published)


def test_ordinary_text_is_not_a_marker(tmp_path):
    """Prose and code that share words with a marker are not refused."""
    _write(
        tmp_path,
        "tests/sample.py",
        "\n".join([
            "# a real customer could override this",
            "# Real customer quotes are added when the owner opts in.",
            "real_customers: list[str] = []",
            "### Lesson 1: Duplicate admin surfaces",
            "expect(text).toContain('REVISION REQUEST (round 1 of 3)')",
            "transition: background 0.18s;",
        ]),
    )
    assert find_markers(tmp_path) == []


# ---- Placeless conventions in the v2 sample fixtures ----

RESERVED_HOSTS = {"example.com", "example.org", "example.net"}
# The fixtures take their placeholder photographs from this service.
PLACEHOLDER_IMAGE_HOST = "picsum.photos"

_HOST_RE = re.compile(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})|https?://([^/\"\s?#]+)")
_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b|\btel:\+?\d{7,11}\b")
_STATE_CODE_RE = re.compile(r",\s*([A-Z]{2})\b")


def host_is_reserved(host: str) -> bool:
    """True for a name nobody can register (RFC 2606 and RFC 6761) and for
    the placeholder image service."""
    host = host.lower().rstrip(".")
    return (
        host in RESERVED_HOSTS
        or host.endswith(".example")
        or host == PLACEHOLDER_IMAGE_HOST
    )


def phone_is_fictional(text: str) -> bool:
    """True for area code 555 with a number in the 555-0100 to 555-0199
    range, the block set aside for fiction."""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) == 10 and digits.startswith("55555501")


def zips_are_a_placeholder_run(zips: list[str]) -> bool:
    """True when the distinct codes are exactly 00001, 00002, ... with no
    gap, so that no digit of them can have come from a real code."""
    distinct = sorted(set(zips))
    return distinct == [f"{n:05d}" for n in range(1, len(distinct) + 1)]


def _strings(node, key=None):
    """Yield (nearest object key, value) for every string in a JSON value."""
    if isinstance(node, dict):
        for child_key, child in node.items():
            yield from _strings(child, child_key)
    elif isinstance(node, list):
        for child in node:
            yield from _strings(child, key)
    elif isinstance(node, str):
        yield key, node


def fixture_convention_violations(fixtures_dir: Path) -> list[str]:
    """Return 'file: problem' for every departure from the conventions."""
    problems: list[str] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        zips: list[str] = []
        for key, value in _strings(json.loads(path.read_text(encoding="utf-8"))):
            for match in _HOST_RE.finditer(value):
                host = match.group(1) or match.group(2)
                if not host_is_reserved(host):
                    problems.append(f"{path.name}: host {host}")
            for match in _PHONE_RE.finditer(value):
                if not phone_is_fictional(match.group(0)):
                    problems.append(f"{path.name}: phone {match.group(0)}")
            if key == "state" and value not in ("", "ST"):
                problems.append(f"{path.name}: state {value}")
            if key and "address" in key:
                for code in _STATE_CODE_RE.findall(value):
                    if code != "ST":
                        problems.append(f"{path.name}: state code in {key}: {code}")
            if key == "zip_codes":
                zips.extend(re.findall(r"\d+", value))
        if not zips_are_a_placeholder_run(zips):
            problems.append(f"{path.name}: postal codes are not a run from 00001")
    return problems


def test_fixtures_keep_the_placeless_conventions():
    assert list(FIXTURES_DIR.glob("*.json")), f"no fixtures under {FIXTURES_DIR}"
    problems = fixture_convention_violations(FIXTURES_DIR)
    assert not problems, "sample fixtures left the conventions:\n" + "\n".join(problems)


def test_convention_checks_report_each_kind_of_departure(tmp_path):
    """The checker must see each kind of departure, and must accept a
    fixture that keeps the conventions. The departing values below are
    themselves invented: a host under the reserved `.test` name, a number
    in the fictional range behind a made-up area code, the state "XX"."""
    clean = {
        "admin_email": "admin@sample-shop.example",
        "settings": {
            "contact_phone": "(555) 555-0101",
            "contact_address": "100 Example St, Sample City, ST",
            "instagram_url": "https://instagram.example/sampleshop",
            "hero_image_url": "https://picsum.photos/seed/shop/800/600",
        },
        "pages": {"home": {"cta": {"href": "tel:5555550101"}}},
        "seed_content": {
            "service_areas": [
                {"city": "Main City", "state": "ST", "zip_codes": ["00001", "00002"]},
                {"city": "Nearby Town", "state": "", "zip_codes": "00003"},
            ],
        },
    }
    (tmp_path / "clean.json").write_text(json.dumps(clean), encoding="utf-8")
    assert fixture_convention_violations(tmp_path) == []

    departing = {
        "admin_email": "admin@sample-shop.test",
        "settings": {
            "contact_phone": "(999) 555-0101",
            "contact_address": "100 Example St, Sample City, XX",
            "site_url": "https://sample-shop.test/about",
        },
        "seed_content": {
            "service_areas": [
                {"city": "Main City", "state": "XX", "zip_codes": ["00002", "00042"]},
            ],
        },
    }
    (tmp_path / "clean.json").unlink()
    (tmp_path / "departing.json").write_text(json.dumps(departing), encoding="utf-8")
    assert fixture_convention_violations(tmp_path) == [
        "departing.json: host sample-shop.test",
        "departing.json: phone (999) 555-0101",
        "departing.json: state code in contact_address: XX",
        "departing.json: host sample-shop.test",
        "departing.json: state XX",
        "departing.json: postal codes are not a run from 00001",
    ]


# ---- Placeless conventions in the Medicine Waves data files ----

DATA_FILE_GLOBS = ("tests/fixtures/*.json", "tests/golden/*/spec.json")
SAMPLE_EVENT_YEAR = "2030"
# A street number, one to four street words, a comma, up to 80 characters, then
# a two-letter state and a five-digit postal code. The first group is the code.
_STREET_ADDRESS_RE = re.compile(
    r'\b\d{1,6} [A-Za-z][A-Za-z.]*(?: [A-Za-z.]+){0,3}, [^\n"]{0,80}?\b[A-Z]{2} (\d{5})(?:-\d{4})?\b'
)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A month and a day, then a clock time within 20 characters of them.
_DATED_TIME_RE = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}\b[^\n"]{0,20}?\b\d{1,2}:\d{2}\b'
)


def data_file_violations(root: Path) -> list[str]:
    """Return 'file: problem' for each Medicine Waves data file under root that
    departs from the placeless conventions. Values are not echoed back, so a
    failure report does not repeat the data it found."""
    problems: list[str] = []
    paths = sorted({path for pattern in DATA_FILE_GLOBS for path in root.glob(pattern)})
    for path in paths:
        rel = path.relative_to(root).as_posix()
        for key, value in _strings(json.loads(path.read_text(encoding="utf-8"))):
            where = key or "a string"
            if any(not code.startswith("000") for code in _STREET_ADDRESS_RE.findall(value)):
                problems.append(
                    f"{rel}: {where} holds a street address with a postal code "
                    "outside the placeholder run"
                )
            for match in _PHONE_RE.finditer(value):
                if not phone_is_fictional(match.group(0)):
                    problems.append(f"{rel}: {where} holds a phone number outside the 555-01xx block")
            if key == "date_time" and _ISO_DATE_RE.match(value) and not value.startswith(SAMPLE_EVENT_YEAR + "-"):
                problems.append(f"{rel}: date_time is not a sample date in {SAMPLE_EVENT_YEAR}")
    return problems


def _write_json(root: Path, rel: str, data: dict) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")


def test_data_files_keep_the_placeless_conventions():
    found = [path for pattern in DATA_FILE_GLOBS for path in REPO_ROOT.glob(pattern)]
    assert found, f"no Medicine Waves data files under {REPO_ROOT}"
    problems = data_file_violations(REPO_ROOT)
    assert not problems, "data files left the sample conventions:\n" + "\n".join(problems)


def test_data_file_checks_report_each_kind_of_departure(tmp_path):
    """The data-file checks must see each kind of departure and must accept a
    file that keeps the conventions. The departing values are invented: a
    street whose postal code (99999) is unassigned, a phone number behind the
    unused area code 999, and an event start in 2026."""
    clean = {
        "_seed_events": [
            {
                "title": "Sample Sound Journey",
                "location": "Example Venue, 100 Example Road, Sample City, ST 00001",
                "date_time": "2030-01-12T00:00:00Z",
            }
        ],
        "_seed_settings": [
            {"key": "contact_phone", "value": "(555) 555-0100"},
            {"key": "contact_address", "value": "Sample City, ST"},
        ],
    }
    _write_json(tmp_path, "tests/fixtures/clean.json", clean)
    _write_json(tmp_path, "tests/golden/sample-set/spec.json", clean)
    assert data_file_violations(tmp_path) == []

    departing = {
        "_seed_events": [
            {
                "title": "Sample Sound Journey",
                "location": "Sample Hall, 100 Oak Lane, Nowhere City, ZZ 99999",
                "date_time": "2026-04-07T00:00:00Z",
            }
        ],
        "_seed_settings": [{"key": "contact_phone", "value": "(999) 555-0177"}],
    }
    (tmp_path / "tests/fixtures/clean.json").unlink()
    (tmp_path / "tests/golden/sample-set/spec.json").unlink()
    _write_json(tmp_path, "tests/fixtures/departing.json", departing)
    assert data_file_violations(tmp_path) == [
        "tests/fixtures/departing.json: location holds a street address with a postal code outside the placeholder run",
        "tests/fixtures/departing.json: date_time is not a sample date in 2030",
        "tests/fixtures/departing.json: value holds a phone number outside the 555-01xx block",
    ]


def test_failure_kb_signatures_carry_no_dated_event_time():
    """A failure signature is a regression rule that ships as an example. Its
    text names no calendar day with a clock time, the shape a dated event start
    takes. The check reports line numbers, not the text it found."""
    text = (REPO_ROOT / "cli" / "failure_kb.json").read_text(encoding="utf-8")
    lines = [
        str(number)
        for number, line in enumerate(text.split("\n"), start=1)
        if _DATED_TIME_RE.search(line)
    ]
    assert not lines, "cli/failure_kb.json names a dated time on lines: " + ", ".join(lines)
