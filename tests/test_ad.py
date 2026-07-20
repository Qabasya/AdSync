"""Тесты `AdGateway` (`ad.py`) на `ldap3` `MOCK_SYNC` — без сети, без реального AD."""

from datetime import UTC, datetime

import pytest
from ldap3 import BASE, MOCK_SYNC, Connection, Server
from ldap3.core.exceptions import LDAPSocketOpenError

from ad import AdGateway, ManagedZoneConfigError, OutsideManagedZoneError
from config import SubjectConfig

ADMIN_DN = "cn=admin,dc=fs,dc=loc"
SUBJECT_OU = "OU=Ученики,DC=fs,DC=loc"
GROUP_DN = "CN=KEGE,OU=Группы,DC=fs,DC=loc"
OU_DISABLED = "OU=Отчисленные,DC=fs,DC=loc"
OU_FALLBACK = "OU=Без направления,DC=fs,DC=loc"
OUTSIDE_DN = "CN=Foreign User,OU=Чужое,DC=fs,DC=loc"

SUBJECTS = {"inf-ege": SubjectConfig(ou_dn=SUBJECT_OU, group_dn=GROUP_DN)}


def _make_connection() -> Connection:
    server = Server("fake")
    connection = Connection(
        server,
        user=ADMIN_DN,
        password="x",
        client_strategy=MOCK_SYNC,
        raise_exceptions=True,
    )
    connection.strategy.add_entry(ADMIN_DN, {"userPassword": "x"})
    connection.bind()
    connection.strategy.add_entry(SUBJECT_OU, {"objectClass": ["organizationalUnit"]})
    connection.strategy.add_entry(GROUP_DN, {"objectClass": ["group"]})
    connection.strategy.add_entry(OU_DISABLED, {"objectClass": ["organizationalUnit"]})
    connection.strategy.add_entry(OU_FALLBACK, {"objectClass": ["organizationalUnit"]})
    connection.strategy.add_entry(
        OUTSIDE_DN,
        {"objectClass": ["user"], "sAMAccountName": "foreign", "userAccountControl": 512},
    )
    return connection


def _make_gateway(connection: Connection | None = None) -> AdGateway:
    return AdGateway(
        connection or _make_connection(),
        reconnect=_make_connection,
        subjects=SUBJECTS,
        ou_disabled=OU_DISABLED,
        ou_fallback=OU_FALLBACK,
        upn_suffix="fs.loc",
    )


def test_find_user_returns_none_when_not_found() -> None:
    gateway = _make_gateway()
    assert gateway.find_user("nope") is None


def test_find_user_returns_dn_and_enabled_state() -> None:
    gateway = _make_gateway()
    dn = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")

    user = gateway.find_user("i.petrov")

    assert user is not None
    assert user.dn == dn
    assert user.enabled is True


def test_create_user_is_idempotent() -> None:
    gateway = _make_gateway()
    dn1 = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")
    dn2 = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")
    assert dn1 == dn2


def test_ensure_password_does_not_raise() -> None:
    gateway = _make_gateway()
    dn = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")
    gateway.ensure_password(dn, "NewPass123")


def test_ensure_enabled_and_disabled_toggle_state() -> None:
    gateway = _make_gateway()
    dn = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")

    gateway.ensure_disabled(dn)
    user = gateway.find_user("i.petrov")
    assert user is not None
    assert user.enabled is False

    gateway.ensure_enabled(dn)
    user = gateway.find_user("i.petrov")
    assert user is not None
    assert user.enabled is True


def test_ensure_group_membership_is_idempotent() -> None:
    connection = _make_connection()
    gateway = _make_gateway(connection)
    dn = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")

    gateway.ensure_group_membership(dn, GROUP_DN)
    gateway.ensure_group_membership(dn, GROUP_DN)

    connection.search(GROUP_DN, "(objectClass=*)", BASE, attributes=["member"])
    members = list(connection.entries[0]["member"].values)
    assert members.count(dn) == 1


def test_move_to_ou_moves_and_is_idempotent() -> None:
    gateway = _make_gateway()
    dn = gateway.create_user(ou_dn=SUBJECT_OU, username="i.petrov", first="Иван", last="Петров")

    new_dn = gateway.move_to_ou(dn, OU_DISABLED)
    assert new_dn.endswith(OU_DISABLED)
    user = gateway.find_user("i.petrov")
    assert user is not None
    assert user.dn == new_dn

    same_dn = gateway.move_to_ou(new_dn, OU_DISABLED)
    assert same_dn == new_dn


def test_write_operations_raise_outside_managed_zone() -> None:
    gateway = _make_gateway()

    with pytest.raises(OutsideManagedZoneError):
        gateway.ensure_password(OUTSIDE_DN, "x")
    with pytest.raises(OutsideManagedZoneError):
        gateway.ensure_enabled(OUTSIDE_DN)
    with pytest.raises(OutsideManagedZoneError):
        gateway.ensure_group_membership(OUTSIDE_DN, GROUP_DN)
    with pytest.raises(OutsideManagedZoneError):
        gateway.move_to_ou(OUTSIDE_DN, OU_DISABLED)

    user = gateway.find_user("foreign")
    assert user is not None
    assert user.enabled is True
    assert user.dn == OUTSIDE_DN


def test_zone_classification() -> None:
    gateway = _make_gateway()
    user_dn = f"CN=Ivan Petrov,{SUBJECT_OU}"
    disabled_user_dn = f"CN=Old User,{OU_DISABLED}"

    assert gateway.is_in_managed_zone(user_dn) is True
    assert gateway.is_in_disabled_ou(user_dn) is False
    assert gateway.is_in_managed_zone(OUTSIDE_DN) is False
    assert gateway.is_in_managed_zone(disabled_user_dn) is True
    assert gateway.is_in_disabled_ou(disabled_user_dn) is True


def test_verify_zone_exists_passes_when_all_present() -> None:
    gateway = _make_gateway()
    gateway.verify_zone_exists()


def test_verify_zone_exists_raises_listing_all_missing_dns() -> None:
    connection = _make_connection()
    missing_group = "CN=Missing Group,OU=Группы,DC=fs,DC=loc"
    missing_fallback = "OU=Тоже нет,DC=fs,DC=loc"
    gateway = AdGateway(
        connection,
        reconnect=_make_connection,
        subjects={"inf-ege": SubjectConfig(ou_dn=SUBJECT_OU, group_dn=missing_group)},
        ou_disabled=OU_DISABLED,
        ou_fallback=missing_fallback,
        upn_suffix="fs.loc",
    )

    with pytest.raises(ManagedZoneConfigError) as exc_info:
        gateway.verify_zone_exists()

    message = str(exc_info.value)
    assert missing_group in message
    assert missing_fallback in message


class _FlakyOnceConnection:
    """Оборачивает `Connection`: первый вызов `search` рвётся, дальше работает как обычно."""

    def __init__(self, inner: Connection) -> None:
        self._inner = inner
        self._search_calls = 0

    def search(self, *args: object, **kwargs: object) -> bool:
        self._search_calls += 1
        if self._search_calls == 1:
            raise LDAPSocketOpenError("connection lost")
        return bool(self._inner.search(*args, **kwargs))

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_reconnect_retries_once_after_communication_error() -> None:
    primary = _FlakyOnceConnection(_make_connection())
    fresh = _make_connection()
    user_dn = f"CN=Ivan Petrov,{SUBJECT_OU}"
    fresh.strategy.add_entry(
        user_dn,
        {"objectClass": ["user"], "sAMAccountName": "i.petrov", "userAccountControl": 512},
    )
    reconnect_calls = 0

    def reconnect() -> Connection:
        nonlocal reconnect_calls
        reconnect_calls += 1
        return fresh

    gateway = AdGateway(
        primary,  # type: ignore[arg-type]
        reconnect=reconnect,
        subjects=SUBJECTS,
        ou_disabled=OU_DISABLED,
        ou_fallback=OU_FALLBACK,
        upn_suffix="fs.loc",
    )

    user = gateway.find_user("i.petrov")

    assert reconnect_calls == 1
    assert user is not None
    assert user.dn == user_dn


def test_list_zone_accounts_covers_zone_and_fallback_excludes_disabled_and_outside() -> None:
    connection = _make_connection()
    gateway = _make_gateway(connection)
    connection.strategy.add_entry(
        f"CN=Subject User,{SUBJECT_OU}",
        {
            "objectClass": ["user"],
            "sAMAccountName": "subject-user",
            "userAccountControl": 512,
            "whenCreated": b"20200101000000.0Z",
        },
    )
    connection.strategy.add_entry(
        f"CN=Fallback User,{OU_FALLBACK}",
        {
            "objectClass": ["user"],
            "sAMAccountName": "fallback-user",
            "userAccountControl": 514,
            "whenCreated": b"20210605120000Z",
        },
    )
    connection.strategy.add_entry(
        f"CN=Disabled User,{OU_DISABLED}",
        {
            "objectClass": ["user"],
            "sAMAccountName": "disabled-user",
            "userAccountControl": 514,
            "whenCreated": b"20200101000000.0Z",
        },
    )

    accounts = gateway.list_zone_accounts()

    usernames = {account.username for account in accounts}
    assert usernames == {"subject-user", "fallback-user"}

    subject_account = next(a for a in accounts if a.username == "subject-user")
    assert subject_account.enabled is True
    assert subject_account.created_at == datetime(2020, 1, 1, tzinfo=UTC)

    fallback_account = next(a for a in accounts if a.username == "fallback-user")
    assert fallback_account.enabled is False
    assert fallback_account.created_at == datetime(2021, 6, 5, 12, 0, 0, tzinfo=UTC)
