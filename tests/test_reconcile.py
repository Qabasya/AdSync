"""Тесты сверки (`reconcile.py`) на фейках, без сети/AD."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeDirectoryGateway, FakeLmsApi

from reconcile import Reconciler

OU_SUBJECT = "OU=KEGE,OU=Ученики,DC=fs,DC=loc"
OU_DISABLED = "OU=Отчисленные,DC=fs,DC=loc"
OU_FALLBACK = "OU=Без направления,DC=fs,DC=loc"

_NOW = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
_OLD = datetime(2000, 1, 1, tzinfo=UTC)


class FailingLmsApi(FakeLmsApi):
    """Фейк LMS, у которого `get_active_usernames` всегда падает с сетевой ошибкой."""

    def get_active_usernames(self) -> list[str]:
        raise RuntimeError("network down")


def make_directory() -> FakeDirectoryGateway:
    return FakeDirectoryGateway(
        zone_ou_dns={OU_SUBJECT}, ou_disabled=OU_DISABLED, ou_fallback=OU_FALLBACK
    )


def make_reconciler(
    directory: FakeDirectoryGateway,
    lms: FakeLmsApi,
    *,
    max_disable: int = 10,
    max_disable_pct: int = 100,
    grace_minutes: int = 15,
) -> Reconciler:
    return Reconciler(
        lms,
        directory,
        ou_disabled=OU_DISABLED,
        max_disable=max_disable,
        max_disable_pct=max_disable_pct,
        grace_minutes=grace_minutes,
        now=lambda: _NOW,
    )


def test_disables_only_accounts_missing_from_active_list() -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD)
    directory.create_user(ou_dn=OU_SUBJECT, username="b", first="B", last="B", created_at=_OLD)
    directory.create_user(ou_dn=OU_SUBJECT, username="c", first="C", last="C", created_at=_OLD)
    lms = FakeLmsApi(active_usernames=["a", "b"])
    reconciler = make_reconciler(directory, lms)

    result = reconciler.run_once()

    assert result.aborted is False
    assert result.disabled_usernames == ("c",)
    assert directory.users_by_username["c"].enabled is False
    assert directory.users_by_username["c"].dn.endswith(OU_DISABLED)
    assert directory.users_by_username["a"].enabled is True
    assert directory.users_by_username["b"].enabled is True


def test_no_action_when_all_accounts_confirmed() -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD)
    lms = FakeLmsApi(active_usernames=["a"])
    reconciler = make_reconciler(directory, lms)

    result = reconciler.run_once()

    assert result.aborted is False
    assert result.disabled_usernames == ()
    assert directory.users_by_username["a"].enabled is True


def test_empty_active_list_with_nonempty_zone_aborts(caplog: pytest.LogCaptureFixture) -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD)
    lms = FakeLmsApi(active_usernames=[])
    reconciler = make_reconciler(directory, lms)

    with caplog.at_level(logging.ERROR, logger="adsync.reconcile"):
        result = reconciler.run_once()

    assert result.aborted is True
    assert result.disabled_usernames == ()
    assert directory.users_by_username["a"].enabled is True
    assert "Сверка отменена" in caplog.text


def test_max_disable_threshold_aborts() -> None:
    directory = make_directory()
    for name in ("a", "b", "c"):
        directory.create_user(
            ou_dn=OU_SUBJECT, username=name, first=name, last=name, created_at=_OLD
        )
    # непустой active-список из постороннего логина -> все три учётки зоны считаются "лишними"
    lms = FakeLmsApi(active_usernames=["someone-else"])
    reconciler = make_reconciler(directory, lms, max_disable=2, max_disable_pct=100)

    result = reconciler.run_once()

    assert result.aborted is True
    assert result.disabled_usernames == ()
    assert all(user.enabled for user in directory.users_by_username.values())


def test_max_disable_pct_threshold_aborts() -> None:
    directory = make_directory()
    for name in ("a", "b", "c", "d"):
        directory.create_user(
            ou_dn=OU_SUBJECT, username=name, first=name, last=name, created_at=_OLD
        )
    # 2 из 4 лишних = 50% > порога 20%, но меньше max_disable по количеству
    lms = FakeLmsApi(active_usernames=["a", "b"])
    reconciler = make_reconciler(directory, lms, max_disable=10, max_disable_pct=20)

    result = reconciler.run_once()

    assert result.aborted is True
    assert result.disabled_usernames == ()
    assert all(user.enabled for user in directory.users_by_username.values())


def test_grace_period_excludes_recent_account_from_stale_count() -> None:
    directory = make_directory()
    recent = _NOW - timedelta(minutes=5)
    directory.create_user(
        ou_dn=OU_SUBJECT, username="fresh", first="F", last="F", created_at=recent
    )
    directory.create_user(ou_dn=OU_SUBJECT, username="old", first="O", last="O", created_at=_OLD)
    directory.create_user(
        ou_dn=OU_SUBJECT, username="confirmed", first="C", last="C", created_at=_OLD
    )
    lms = FakeLmsApi(active_usernames=["confirmed"])
    reconciler = make_reconciler(directory, lms, grace_minutes=15)

    result = reconciler.run_once()

    assert result.aborted is False
    assert result.disabled_usernames == ("old",)
    assert directory.users_by_username["fresh"].enabled is True
    assert directory.users_by_username["confirmed"].enabled is True


def test_get_active_usernames_failure_aborts(caplog: pytest.LogCaptureFixture) -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD)
    lms = FailingLmsApi()
    reconciler = make_reconciler(directory, lms)

    with caplog.at_level(logging.ERROR, logger="adsync.reconcile"):
        result = reconciler.run_once()

    assert result.aborted is True
    assert directory.users_by_username["a"].enabled is True
    assert "не удалось получить список активных логинов" in caplog.text


def test_reconcile_is_one_directional_never_enables_or_creates() -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD)
    lms = FakeLmsApi(active_usernames=[])
    reconciler = make_reconciler(directory, lms)

    reconciler.run_once()

    assert set(directory.users_by_username) == {"a"}
    assert directory.group_members == {}
