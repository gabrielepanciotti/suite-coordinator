"""#85: day-of-week in standard cron semantics (0/7 = sunday), not APScheduler's."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import notifications

TZ = ZoneInfo("Europe/Rome")
# Monday 2026-10-19 12:00
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)


def _next(schedule: str) -> datetime:
    return notifications.cron_trigger(schedule, timezone=TZ).get_next_fire_time(None, NOW)


@pytest.mark.parametrize("dow", ["0", "7", "sun"])
def test_sunday(dow):
    nxt = _next(f"0 18 * * {dow}")
    assert nxt.strftime("%a %Y-%m-%d %H:%M") == "Sun 2026-10-25 18:00"


def test_one_is_monday():
    # monday 12:00 now -> 09:30 already gone -> next monday
    assert _next("30 9 * * 1").strftime("%a %d") == "Mon 26"


def test_weekdays_range():
    days = set()
    trig = notifications.cron_trigger("0 8 * * 1-5", timezone=TZ)
    cur = NOW
    for _ in range(10):
        cur = trig.get_next_fire_time(None, cur)
        days.add(cur.strftime("%a"))
        cur = cur.replace(minute=1)
    assert days == {"Mon", "Tue", "Wed", "Thu", "Fri"}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("0 18 * * 0", "0 18 * * sun"),
        ("0 18 * * 7", "0 18 * * sun"),
        ("0 8 * * 1-5", "0 8 * * mon,tue,wed,thu,fri"),
        ("0 8 * * 5-7", "0 8 * * fri,sat,sun"),
        ("0 8 * * 0,6", "0 8 * * sun,sat"),
        ("0 8 * * */2", "0 8 * * sun,tue,thu,sat"),
        ("0 8 * * 1-5/2", "0 8 * * mon,wed,fri"),
        ("0 8 * * mon-fri", "0 8 * * mon-fri"),
        ("0 8 * * *", "0 8 * * *"),
        ("0 8 1 * sun,1", "0 8 1 * sun,mon"),
    ],
)
def test_translation(given, expected):
    assert notifications.to_apscheduler_crontab(given) == expected


@pytest.mark.parametrize("bad", ["0 8 * * 8", "0 8 * * 5-2", "0 8 * *"])
def test_invalid(bad):
    assert notifications.validate_schedule(bad) is False
