"""Placeholder "upcoming" dates are counted from the build day, not hardcoded.

Scaffolded specs and the placeholder seed rows used to carry literal 2026
dates for their sample events. Once those dates passed, a freshly composed
site listed its sample events as past and its "upcoming" sections rendered
empty. These tests pin the clock with SOURCE_DATE_EPOCH (the same variable
that makes a build reproducible) and check the dates move with it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from compose.build import _placeholder_rows, _sample_date
from compose.intake import scaffold_spec

_ANSWERS = {
    "app_name": "demo-site",
    "site_name": "Demo Site",
    "admin_email": "admin@demo.example",
}


def _epoch(day: date) -> str:
    return str(int(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()))


@pytest.fixture()
def pinned_day(monkeypatch):
    def pin(day: date) -> date:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", _epoch(day))
        return day
    return pin


def test_sample_date_counts_forward_from_the_pinned_build_day(pinned_day):
    day = pinned_day(date(2031, 2, 27))
    assert _sample_date(0) == "2031-02-27"
    assert _sample_date(14) == "2031-03-13"
    assert _sample_date(30) == (day + timedelta(days=30)).isoformat()


def test_sample_date_without_a_pinned_epoch_uses_today_in_utc(monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    before = datetime.now(timezone.utc).date()
    got = date.fromisoformat(_sample_date(10))
    after = datetime.now(timezone.utc).date()
    assert before + timedelta(days=10) <= got <= after + timedelta(days=10)


@pytest.mark.parametrize("build_day", [date(2026, 10, 10), date(2029, 1, 31), date(2040, 12, 31)])
def test_event_producer_sample_events_are_all_upcoming(pinned_day, build_day):
    pinned_day(build_day)
    spec = scaffold_spec({**_ANSWERS, "archetype": "event-producer"})
    dates = [e["event_date"] for e in spec["seed_content"]["events"]]
    assert len(dates) == 4
    assert dates == sorted(dates) and len(set(dates)) == 4
    assert all(date.fromisoformat(d) > build_day for d in dates), dates
    # The same dates feed the home and events pages, not a second copy.
    home_dates = [i["date"] for i in spec["pages"]["home"]["upcoming_events"]["items"]]
    assert home_dates == dates


@pytest.mark.parametrize("archetype", ["healer-wellness", "author"])
def test_other_archetypes_sample_event_is_upcoming(pinned_day, archetype):
    build_day = pinned_day(date(2033, 6, 15))
    spec = scaffold_spec({**_ANSWERS, "archetype": archetype})
    (event,) = spec["seed_content"]["events"]
    assert date.fromisoformat(event["event_date"]) > build_day


def test_scaffolded_dates_follow_the_build_day_and_are_stable_for_one_day(pinned_day):
    pinned_day(date(2030, 1, 5))
    first = scaffold_spec({**_ANSWERS, "archetype": "event-producer"})
    again = scaffold_spec({**_ANSWERS, "archetype": "event-producer"})
    assert first == again
    pinned_day(date(2030, 7, 5))
    later = scaffold_spec({**_ANSWERS, "archetype": "event-producer"})
    assert later["seed_content"]["events"] != first["seed_content"]["events"]


def test_placeholder_event_row_is_upcoming(pinned_day):
    build_day = pinned_day(date(2035, 3, 1))
    (row,) = _placeholder_rows("Event")
    assert date.fromisoformat(row["event_date"]) > build_day
