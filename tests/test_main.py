"""Тесты чистой логики `main.py` (`_seconds_until_next_summary`).

`main.py` в целом не покрывается pytest — composition root требует реального LDAPS/uvicorn,
как и `build_ldaps_connection` (`ad.py`, этап 4). Здесь тестируется только чистая функция.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from main import _seconds_until_next_summary

_TZ = ZoneInfo("Europe/Moscow")


def test_returns_seconds_until_todays_target_when_not_yet_reached() -> None:
    now = datetime(2026, 7, 20, 10, 0, 0, tzinfo=_TZ)

    seconds = _seconds_until_next_summary("18:00", _TZ, now)

    assert seconds == 8 * 3600


def test_returns_seconds_until_tomorrow_when_today_already_passed() -> None:
    now = datetime(2026, 7, 20, 20, 0, 0, tzinfo=_TZ)

    seconds = _seconds_until_next_summary("18:00", _TZ, now)

    assert seconds == 22 * 3600


def test_exact_match_rolls_over_to_tomorrow_to_avoid_double_fire() -> None:
    now = datetime(2026, 7, 20, 18, 0, 0, tzinfo=_TZ)

    seconds = _seconds_until_next_summary("18:00", _TZ, now)

    assert seconds == 24 * 3600


def test_converts_across_timezones() -> None:
    now_utc = datetime(2026, 7, 20, 5, 0, 0, tzinfo=UTC)  # 08:00 в Europe/Moscow (UTC+3)

    seconds = _seconds_until_next_summary("09:00", _TZ, now_utc)

    assert seconds == 3600
