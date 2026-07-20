"""LDAPS-шлюз к Active Directory: примитивные идемпотентные операции + guard управляемой зоны.

Бизнес-ветвление (что делать, если учётка уже существует / в «Отчисленных» / вне зоны) здесь не
живёт — это `handlers.py`. Здесь только сами LDAP-действия, контроль зоны и переподключение.
"""

import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from ldap3 import BASE, MODIFY_ADD, MODIFY_REPLACE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import (
    LDAPCommunicationError,
    LDAPEntryAlreadyExistsResult,
    LDAPNoSuchObjectResult,
)
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn, parse_dn

from config import SubjectConfig

logger = logging.getLogger("adsync.ad")

_ACCOUNT_ENABLED = 512
_ACCOUNT_DISABLED = 514
_ACCOUNTDISABLE_BIT = 0x2
_USER_OBJECT_CLASSES = ["top", "person", "organizationalPerson", "user"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    """Найденная в AD учётная запись."""

    dn: str
    enabled: bool


class OutsideManagedZoneError(Exception):
    """Запрошена операция над объектом вне управляемой зоны — объект не тронут."""


class ManagedZoneConfigError(Exception):
    """На старте в AD не найден один или несколько DN из конфигурации."""


def _dn_components(dn: str) -> list[tuple[str, str]]:
    return [(attr.lower(), value.lower()) for attr, value, _ in parse_dn(dn)]


def _is_within_ou(dn: str, ou_dn: str) -> bool:
    dn_parts = _dn_components(dn)
    ou_parts = _dn_components(ou_dn)
    if len(dn_parts) <= len(ou_parts):
        return False
    return dn_parts[-len(ou_parts) :] == ou_parts


def _dn_equals(left: str, right: str) -> bool:
    return _dn_components(left) == _dn_components(right)


def _parent_ou(dn: str) -> str:
    parts = parse_dn(dn)
    return ",".join(f"{attr}={escape_rdn(value)}" for attr, value, _ in parts[1:])


class DirectoryGateway(Protocol):
    """Контракт похода в AD — реализуется боевым `AdGateway` и тестовым `FakeDirectoryGateway`."""

    def find_user(self, username: str) -> DirectoryUser | None:
        """Ищет учётку по `sAMAccountName` от корня домена (не только в управляемой зоне)."""
        ...

    def is_in_managed_zone(self, dn: str) -> bool:
        """DN входит в зону: OU направлений, `AD_OU_FALLBACK` или `AD_OU_DISABLED`."""
        ...

    def is_in_disabled_ou(self, dn: str) -> bool:
        """DN находится конкретно в OU «Отчисленные»."""
        ...

    def create_user(self, *, ou_dn: str, username: str, first: str, last: str) -> str:
        """Создаёт включённую учётку в `ou_dn`. Идемпотентно. Возвращает DN. Без пароля."""
        ...

    def ensure_password(self, dn: str, password: str) -> None:
        """Задаёт пароль через `extend.microsoft.modify_password` (только LDAPS)."""
        ...

    def ensure_enabled(self, dn: str) -> None:
        """Включает учётку (`userAccountControl=512`)."""
        ...

    def ensure_disabled(self, dn: str) -> None:
        """Отключает учётку (`userAccountControl=514`)."""
        ...

    def ensure_group_membership(self, user_dn: str, group_dn: str) -> None:
        """Добавляет в группу, если ещё не состоит (идемпотентно)."""
        ...

    def move_to_ou(self, dn: str, target_ou_dn: str) -> str:
        """Переносит объект в `target_ou_dn`. Идемпотентно. Возвращает новый DN."""
        ...

    def verify_zone_exists(self) -> None:
        """Стартовая проверка: все DN конфигурации существуют в AD, иначе понятная ошибка."""
        ...


class AdGateway:
    """Боевая реализация `DirectoryGateway` поверх `ldap3`."""

    def __init__(
        self,
        connection: Connection,
        *,
        reconnect: Callable[[], Connection],
        subjects: dict[str, SubjectConfig],
        ou_disabled: str,
        ou_fallback: str,
        upn_suffix: str,
    ) -> None:
        """Создаёт шлюз поверх уже установленного соединения.

        Args:
            connection: LDAP-соединение (боевое — LDAPS с TLS, тестовое — `MOCK_SYNC`). Должно
                быть создано с `raise_exceptions=True`.
            reconnect: возвращает свежее установленное соединение взамен потерянного.
            subjects: карта направлений из `subjects.yaml`.
            ou_disabled: DN OU «Отчисленные».
            ou_fallback: DN OU «Без направления».
            upn_suffix: домен после `@` в UPN; источник корня поиска (`fs.loc` → `DC=fs,DC=loc`).
        """
        self._connection = connection
        self._reconnect = reconnect
        self._ou_disabled = ou_disabled
        self._ou_fallback = ou_fallback
        self._upn_suffix = upn_suffix
        self._zone_dns = [subject.ou_dn for subject in subjects.values()] + [
            ou_disabled,
            ou_fallback,
        ]
        self._config_dns = sorted(
            {subject.ou_dn for subject in subjects.values()}
            | {subject.group_dn for subject in subjects.values()}
            | {ou_disabled, ou_fallback}
        )
        self._domain_root_dn = "DC=" + ",DC=".join(upn_suffix.split("."))

    def _run(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except LDAPCommunicationError as exc:
            logger.warning("Соединение с AD потеряно, переподключаюсь: %s", exc)
            self._connection = self._reconnect()
            return operation()

    def is_in_managed_zone(self, dn: str) -> bool:
        return any(_is_within_ou(dn, zone_dn) for zone_dn in self._zone_dns)

    def is_in_disabled_ou(self, dn: str) -> bool:
        return _is_within_ou(dn, self._ou_disabled)

    def find_user(self, username: str) -> DirectoryUser | None:
        escaped = escape_filter_chars(username)

        def op() -> bool:
            return bool(
                self._connection.search(
                    self._domain_root_dn,
                    f"(sAMAccountName={escaped})",
                    SUBTREE,
                    attributes=["userAccountControl"],
                )
            )

        self._run(op)
        entries = self._connection.entries
        if not entries:
            return None
        uac = int(entries[0]["userAccountControl"].value)
        return DirectoryUser(dn=entries[0].entry_dn, enabled=not bool(uac & _ACCOUNTDISABLE_BIT))

    def create_user(self, *, ou_dn: str, username: str, first: str, last: str) -> str:
        display_name = f"{first} {last}"
        dn = f"CN={escape_rdn(display_name)},{ou_dn}"
        attributes = {
            "sAMAccountName": username,
            "userPrincipalName": f"{username}@{self._upn_suffix}",
            "givenName": first,
            "sn": last,
            "displayName": display_name,
            "userAccountControl": _ACCOUNT_ENABLED,
        }

        def op() -> bool:
            return bool(self._connection.add(dn, _USER_OBJECT_CLASSES, attributes))

        try:
            self._run(op)
        except LDAPEntryAlreadyExistsResult:
            logger.info("Учётная запись %s уже существует, создание пропущено", dn)
        return dn

    def ensure_password(self, dn: str, password: str) -> None:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)

        def op() -> bool:
            result = self._connection.extend.microsoft.modify_password(dn, password)
            return bool(result)

        self._run(op)

    def ensure_enabled(self, dn: str) -> None:
        self._set_account_control(dn, _ACCOUNT_ENABLED)

    def ensure_disabled(self, dn: str) -> None:
        self._set_account_control(dn, _ACCOUNT_DISABLED)

    def _set_account_control(self, dn: str, value: int) -> None:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)

        def op() -> bool:
            return bool(
                self._connection.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [value])]})
            )

        self._run(op)

    def ensure_group_membership(self, user_dn: str, group_dn: str) -> None:
        if not self.is_in_managed_zone(user_dn):
            raise OutsideManagedZoneError(user_dn)

        def search_op() -> bool:
            return bool(
                self._connection.search(group_dn, "(objectClass=*)", BASE, attributes=["member"])
            )

        self._run(search_op)
        entries = self._connection.entries
        members = list(entries[0]["member"].values) if entries else []
        if any(_dn_equals(member, user_dn) for member in members):
            return

        def modify_op() -> bool:
            return bool(self._connection.modify(group_dn, {"member": [(MODIFY_ADD, [user_dn])]}))

        self._run(modify_op)

    def move_to_ou(self, dn: str, target_ou_dn: str) -> str:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)
        if _dn_equals(_parent_ou(dn), target_ou_dn):
            return dn

        rdn_attr, rdn_value, _ = parse_dn(dn)[0]
        new_rdn = f"{rdn_attr}={escape_rdn(rdn_value)}"

        def op() -> bool:
            return bool(self._connection.modify_dn(dn, new_rdn, new_superior=target_ou_dn))

        self._run(op)
        return f"{new_rdn},{target_ou_dn}"

    def verify_zone_exists(self) -> None:
        missing = [dn for dn in self._config_dns if not self._dn_exists(dn)]
        if missing:
            raise ManagedZoneConfigError(
                "в AD не найдены DN из конфигурации: " + ", ".join(missing)
            )

    def _dn_exists(self, dn: str) -> bool:
        def op() -> bool:
            return bool(
                self._connection.search(
                    dn, "(objectClass=*)", BASE, attributes=["distinguishedName"]
                )
            )

        try:
            self._run(op)
        except LDAPNoSuchObjectResult:
            return False
        return bool(self._connection.entries)

    def close(self) -> None:
        """Закрывает LDAP-соединение (graceful shutdown)."""
        self._connection.unbind()


def build_ldaps_connection(
    *, host: str, port: int, ca_cert_path: Path, bind_dn: str, bind_password: str
) -> Connection:
    """Собирает боевое LDAPS-соединение с верификацией сертификата DC по `ca_cert_path`.

    Не покрывается pytest (нужен реальный DC) — используется `main.py` (композиция) и
    `scripts/smoke.py` (bind-check вне тестов).
    """
    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=str(ca_cert_path))
    server = Server(host, port=port, use_ssl=True, tls=tls)
    return Connection(
        server, user=bind_dn, password=bind_password, auto_bind=True, raise_exceptions=True
    )
