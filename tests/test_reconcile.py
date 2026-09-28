"""Тесты сверки (`reconcile.py`) на фейках, без сети/AD."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeDirectoryGateway

from ad import DirectoryUnavailableError
from reconcile import Reconciler

OU_SUBJECT = "OU=KEGE,OU=Ученики,DC=fs,DC=loc"
OU_DISABLED = "OU=Отчисленные,DC=fs,DC=loc"
OU_FALLBACK = "OU=Без направления,DC=fs,DC=loc"

_NOW = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
_OLD = datetime(2000, 1, 1, tzinfo=UTC)


def make_directory() -> FakeDirectoryGateway:
    return FakeDirectoryGateway(
        zone_ou_dns={OU_SUBJECT}, ou_disabled=OU_DISABLED, ou_fallback=OU_FALLBACK
    )


def seed_active_account(
    directory: FakeDirectoryGateway,
    *,
    ou_dn: str,
    username: str,
    first: str,
    last: str,
    created_at: datetime | None = None,
) -> str:
    """Сеет активную учётку в зоне: `create_user` (как и реальный `AdGateway`) создаёт отключённой
    (AD не даёт включить без пароля), поэтому для сценария «уже активная учётка» включаем явно."""
    dn = directory.create_user(
        ou_dn=ou_dn, username=username, first=first, last=last, created_at=created_at
    )
    directory.ensure_enabled(dn)
    return dn


def make_reconciler(
    directory: FakeDirectoryGateway,
    *,
    max_disable: int = 10,
    max_disable_pct: int = 100,
    grace_minutes: int = 15,
) -> Reconciler:
    return Reconciler(
        directory,
        ou_disabled=OU_DISABLED,
        max_disable=max_disable,
        max_disable_pct=max_disable_pct,
        grace_minutes=grace_minutes,
        now=lambda: _NOW,
    )


def test_disables_only_accounts_missing_from_active_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    directory = make_directory()
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD
    )
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="b", first="B", last="B", created_at=_OLD
    )
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="c", first="C", last="C", created_at=_OLD
    )
    active = ["a", "b"]
    reconciler = make_reconciler(directory)

    with caplog.at_level(logging.INFO, logger="adsync.reconcile"):
        result = reconciler.run(active, apply=True)

    assert result.aborted is False
    assert result.disabled_usernames == ("c",)
    assert directory.users_by_username["c"].enabled is False
    assert directory.users_by_username["c"].dn.endswith(OU_DISABLED)
    assert directory.users_by_username["a"].enabled is True
    assert directory.users_by_username["b"].enabled is True
    assert "отключено 1 из 3" in caplog.text


def test_no_action_when_all_accounts_confirmed() -> None:
    directory = make_directory()
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD
    )
    active = ["a"]
    reconciler = make_reconciler(directory)

    result = reconciler.run(active, apply=True)

    assert result.aborted is False
    assert result.disabled_usernames == ()
    assert directory.users_by_username["a"].enabled is True


def test_empty_active_list_with_nonempty_zone_aborts(caplog: pytest.LogCaptureFixture) -> None:
    directory = make_directory()
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD
    )
    active = []
    reconciler = make_reconciler(directory)

    with caplog.at_level(logging.ERROR, logger="adsync.reconcile"):
        result = reconciler.run(active, apply=True)

    assert result.aborted is True
    assert result.disabled_usernames == ()
    assert directory.users_by_username["a"].enabled is True
    assert "Сверка отменена" in caplog.text


def test_max_disable_threshold_aborts() -> None:
    directory = make_directory()
    for name in ("a", "b", "c"):
        seed_active_account(
            directory, ou_dn=OU_SUBJECT, username=name, first=name, last=name, created_at=_OLD
        )
    # непустой active-список из постороннего логина -> все три учётки зоны считаются "лишними"
    active = ["someone-else"]
    reconciler = make_reconciler(directory, max_disable=2, max_disable_pct=100)

    result = reconciler.run(active, apply=True)

    assert result.aborted is True
    assert result.disabled_usernames == ()
    assert all(user.enabled for user in directory.users_by_username.values())


def test_max_disable_pct_threshold_aborts() -> None:
    directory = make_directory()
    for name in ("a", "b", "c", "d"):
        seed_active_account(
            directory, ou_dn=OU_SUBJECT, username=name, first=name, last=name, created_at=_OLD
        )
    # 2 из 4 лишних = 50% > порога 20%, но меньше max_disable по количеству
    active = ["a", "b"]
    reconciler = make_reconciler(directory, max_disable=10, max_disable_pct=20)

    result = reconciler.run(active, apply=True)

    assert result.aborted is True
    assert result.disabled_usernames == ()
    assert all(user.enabled for user in directory.users_by_username.values())


def test_grace_period_excludes_recent_account_from_stale_count() -> None:
    directory = make_directory()
    recent = _NOW - timedelta(minutes=5)
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="fresh", first="F", last="F", created_at=recent
    )
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="old", first="O", last="O", created_at=_OLD
    )
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="confirmed", first="C", last="C", created_at=_OLD
    )
    active = ["confirmed"]
    reconciler = make_reconciler(directory, grace_minutes=15)

    result = reconciler.run(active, apply=True)

    assert result.aborted is False
    assert result.disabled_usernames == ("old",)
    assert directory.users_by_username["fresh"].enabled is True
    assert directory.users_by_username["confirmed"].enabled is True


def test_reconcile_is_one_directional_never_enables_or_creates() -> None:
    directory = make_directory()
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD
    )
    active = []
    reconciler = make_reconciler(directory)

    reconciler.run(active, apply=True)

    assert set(directory.users_by_username) == {"a"}
    assert directory.group_members == {}


def test_dry_run_reports_but_touches_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Режим «только журнал» (первая неделя): кого отключила бы — в ответ и в лог, AD не тронут."""
    directory = make_directory()
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="a", first="A", last="A", created_at=_OLD
    )
    seed_active_account(
        directory, ou_dn=OU_SUBJECT, username="b", first="B", last="B", created_at=_OLD
    )
    reconciler = make_reconciler(directory)

    with caplog.at_level(logging.INFO, logger="adsync.reconcile"):
        result = reconciler.run(["a"], apply=False)

    assert result.aborted is False
    assert result.applied is False
    assert result.disabled_usernames == ("b",)
    assert directory.users_by_username["b"].enabled is True
    assert not directory.users_by_username["b"].dn.endswith(OU_DISABLED)
    assert getattr(caplog.records[-1], "event", None) == "reconcile_dry_run"


def test_dry_run_still_respects_guards() -> None:
    """Предохранитель срабатывает и в режиме «только журнал» — админ увидит abort до включения."""
    directory = make_directory()
    for name in ("a", "b", "c"):
        seed_active_account(
            directory, ou_dn=OU_SUBJECT, username=name, first=name, last=name, created_at=_OLD
        )
    reconciler = make_reconciler(directory, max_disable=1)

    result = reconciler.run(["someone-else"], apply=False)

    assert result.aborted is True
    assert result.disabled_usernames == ()


def test_unavailable_directory_propagates() -> None:
    """DC недоступен — не abort сверки, а исключение: API ответит сайту 503."""
    directory = make_directory()
    directory.unavailable = True
    reconciler = make_reconciler(directory)

    with pytest.raises(DirectoryUnavailableError):
        reconciler.run(["a"], apply=True)
