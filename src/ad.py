"""LDAPS-шлюз к Active Directory: примитивные идемпотентные операции + guard управляемой зоны.

Бизнес-ветвление (что делать, если учётка уже существует / в «Отчисленных» / вне зоны) здесь не
живёт — это `handlers.py`. Здесь только сами LDAP-действия, контроль зоны и переподключение.
"""

import logging
import re
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
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

_WHEN_CREATED_RE = re.compile(r"^(\d{14})(?:\.\d+)?Z$")

# Сколько вариантов CN пробуем, прежде чем сдаться. Второй кандидат уже содержит логин, который
# уникален в домене, поэтому реально дальше него доходит только совсем вырожденный случай
# (объект, названный ровно «Имя Фамилия (логин)», заведённый руками).
_MAX_CN_CANDIDATES = 10


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    """Найденная в AD учётная запись."""

    dn: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ZoneAccount:
    """Учётная запись из массового перечисления управляемой зоны (для сверки)."""

    dn: str
    username: str
    enabled: bool
    created_at: datetime


def _parse_when_created(value: object) -> datetime:
    """Разбирает `whenCreated` в aware `datetime` (UTC).

    На боевом LDAP ldap3 при online-схеме возвращает уже `datetime`; в `MOCK_SYNC`
    и при отсутствии схемы — сырую строку `GeneralizedTime`. Поддерживаем оба случая.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value)
    match = _WHEN_CREATED_RE.match(text)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"неожиданный формат whenCreated: {text!r}") from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class OutsideManagedZoneError(Exception):
    """Запрошена операция над объектом вне управляемой зоны — объект не тронут."""


class ManagedZoneConfigError(Exception):
    """На старте в AD не найден один или несколько DN из конфигурации."""


class CnAllocationError(Exception):
    """В целевой OU не удалось подобрать свободный CN — объект не создан и не перенесён."""


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


def _cn_candidates(display_name: str, username: str) -> Iterator[str]:
    """Варианты CN для объекта: сначала «Имя Фамилия», при занятости — с логином в скобках.

    CN уникален только в пределах своей OU, а `sAMAccountName` — во всём домене. У двух тёзок в
    одном направлении совпадает именно CN, поэтому различать их нужно логином: «Иван Петров» →
    «Иван Петров (i.petrov2)». `displayName` при этом остаётся человеческим у обоих.
    """
    yield display_name
    yield f"{display_name} ({username})"
    for suffix in range(2, _MAX_CN_CANDIDATES):
        yield f"{display_name} ({username} {suffix})"


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
        """Создаёт учётку в `ou_dn`, изначально ОТКЛЮЧЁННОЙ. Идемпотентно. Возвращает DN.

        AD отклоняет создание сразу включённой учётки без пароля (`WILL_NOT_PERFORM`/5003) — пароль
        и включение это отдельные последующие шаги (`ensure_password`, затем `ensure_enabled`).
        """
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

    def list_zone_accounts(self) -> list[ZoneAccount]:
        """Перечисляет учётки во всех OU направлений + `AD_OU_FALLBACK` (без «Отчисленных»)."""
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
        self._reconcile_ou_dns = sorted(
            {subject.ou_dn for subject in subjects.values()} | {ou_fallback}
        )
        self._domain_root_dn = "DC=" + ",DC=".join(upn_suffix.split("."))

    def _run(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except LDAPCommunicationError as exc:
            logger.warning(
                "Соединение с AD потеряно, переподключаюсь: %s",
                exc,
                extra={"event": "ad_reconnect"},
            )
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

    def _log_result(self, action: str, dn: str) -> None:
        result = self._connection.result
        logger.info(
            "%s: %s — LDAP result=%s (%s)",
            action,
            dn,
            result.get("result"),
            result.get("description"),
        )

    def create_user(self, *, ou_dn: str, username: str, first: str, last: str) -> str:
        """Создаёт учётку, подбирая свободный CN, если в OU уже есть тёзка.

        `entryAlreadyExists` здесь означает две разные вещи, и путать их нельзя: либо это наша же
        учётка с прошлой попытки (повтор задания — успех, ничего не делаем), либо это ДРУГОЙ
        человек с тем же ФИО. Во втором случае продолжать работу с чужим DN недопустимо — тёзка
        получает собственный CN с логином в скобках.
        """
        display_name = f"{first} {last}"
        attributes = {
            "sAMAccountName": username,
            "userPrincipalName": f"{username}@{self._upn_suffix}",
            "givenName": first,
            "sn": last,
            "displayName": display_name,
            # Создаём ОТКЛЮЧЁННОЙ: AD отклоняет add() сразу включённой учётки без пароля
            # (WILL_NOT_PERFORM/5003). Включение — отдельным шагом, после ensure_password.
            "userAccountControl": _ACCOUNT_DISABLED,
        }

        for candidate_cn in _cn_candidates(display_name, username):
            dn = f"CN={escape_rdn(candidate_cn)},{ou_dn}"
            try:
                self._run(partial(self._add_user, dn, attributes))
            except LDAPEntryAlreadyExistsResult:
                if self._owns_dn(dn, username):
                    logger.info("учётная запись уже существует, создание пропущено: %s", dn)
                    return dn
                logger.warning(
                    "CN %s в %s занят другой учётной записью, подбираю следующий вариант",
                    candidate_cn,
                    ou_dn,
                    extra={"event": "cn_collision"},
                )
                continue
            self._log_result("создана учётная запись (отключена)", dn)
            return dn

        raise CnAllocationError(
            f"не удалось подобрать свободный CN для {username} в {ou_dn} "
            f"за {_MAX_CN_CANDIDATES} попыток"
        )

    def _add_user(self, dn: str, attributes: dict[str, object]) -> bool:
        return bool(self._connection.add(dn, _USER_OBJECT_CLASSES, attributes))

    def _owns_dn(self, dn: str, username: str) -> bool:
        """Принадлежит ли объект по `dn` учётке `username` (сравнение `sAMAccountName`)."""
        existing = self._read_sam_account_name(dn)
        return existing is not None and existing.casefold() == username.casefold()

    def _read_sam_account_name(self, dn: str) -> str | None:
        """`sAMAccountName` объекта по DN; `None`, если объекта нет или атрибут пуст."""

        def op() -> bool:
            return bool(
                self._connection.search(dn, "(objectClass=*)", BASE, attributes=["sAMAccountName"])
            )

        try:
            self._run(op)
        except LDAPNoSuchObjectResult:
            return None
        entries = self._connection.entries
        if not entries:
            return None
        value = entries[0]["sAMAccountName"].value
        return str(value) if value else None

    def ensure_password(self, dn: str, password: str) -> None:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)

        def op() -> bool:
            result = self._connection.extend.microsoft.modify_password(dn, password)
            return bool(result)

        self._run(op)
        self._log_result("пароль установлен", dn)

    def ensure_enabled(self, dn: str) -> None:
        self._set_account_control(dn, _ACCOUNT_ENABLED, action="учётная запись включена")

    def ensure_disabled(self, dn: str) -> None:
        self._set_account_control(dn, _ACCOUNT_DISABLED, action="учётная запись отключена")

    def _set_account_control(self, dn: str, value: int, *, action: str) -> None:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)

        def op() -> bool:
            return bool(
                self._connection.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [value])]})
            )

        self._run(op)
        self._log_result(action, dn)

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
            logger.info("уже состоит в группе %s: %s", group_dn, user_dn)
            return

        def modify_op() -> bool:
            return bool(self._connection.modify(group_dn, {"member": [(MODIFY_ADD, [user_dn])]}))

        self._run(modify_op)
        self._log_result(f"добавлена в группу {group_dn}", user_dn)

    def move_to_ou(self, dn: str, target_ou_dn: str) -> str:
        """Переносит объект, подбирая свободный CN, если в целевой OU уже есть тёзка.

        Та же ловушка, что и в `create_user`, только на пути реактивации: «Иван Петров» из
        «Отчисленных» едет в OU направления, где такой CN уже занят другим Иваном Петровым, и AD
        отвечает `entryAlreadyExists`. Переносим под CN с логином в скобках вместо падения.
        """
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)
        if _dn_equals(_parent_ou(dn), target_ou_dn):
            logger.info("уже в целевой OU %s: %s", target_ou_dn, dn)
            return dn

        rdn_attr, rdn_value, _ = parse_dn(dn)[0]
        username = self._read_sam_account_name(dn)
        candidates = (
            _cn_candidates(rdn_value, username) if username is not None else iter([rdn_value])
        )

        for candidate_rdn_value in candidates:
            new_rdn = f"{rdn_attr}={escape_rdn(candidate_rdn_value)}"
            try:
                self._run(partial(self._move, dn, new_rdn, target_ou_dn))
            except LDAPEntryAlreadyExistsResult:
                logger.warning(
                    "CN %s в %s занят другой учётной записью, подбираю следующий вариант",
                    candidate_rdn_value,
                    target_ou_dn,
                    extra={"event": "cn_collision"},
                )
                continue
            new_dn = f"{new_rdn},{target_ou_dn}"
            self._log_result(f"перенесена в {target_ou_dn}", new_dn)
            return new_dn

        raise CnAllocationError(
            f"не удалось подобрать свободный CN для переноса {dn} в {target_ou_dn} "
            f"за {_MAX_CN_CANDIDATES} попыток"
        )

    def _move(self, dn: str, new_rdn: str, target_ou_dn: str) -> bool:
        return bool(self._connection.modify_dn(dn, new_rdn, new_superior=target_ou_dn))

    def verify_zone_exists(self) -> None:
        missing = [dn for dn in self._config_dns if not self._dn_exists(dn)]
        if missing:
            raise ManagedZoneConfigError(
                "в AD не найдены DN из конфигурации: " + ", ".join(missing)
            )

    def list_zone_accounts(self) -> list[ZoneAccount]:
        accounts: dict[str, ZoneAccount] = {}
        for ou_dn in self._reconcile_ou_dns:
            self._run(partial(self._search_zone_ou, ou_dn))
            for entry in self._connection.entries:
                uac = int(entry["userAccountControl"].value)
                accounts[entry.entry_dn] = ZoneAccount(
                    dn=entry.entry_dn,
                    username=str(entry["sAMAccountName"].value),
                    enabled=not bool(uac & _ACCOUNTDISABLE_BIT),
                    created_at=_parse_when_created(entry["whenCreated"].value),
                )
        return list(accounts.values())

    def _search_zone_ou(self, ou_dn: str) -> bool:
        return bool(
            self._connection.search(
                ou_dn,
                "(objectClass=user)",
                SUBTREE,
                attributes=["sAMAccountName", "userAccountControl", "whenCreated"],
            )
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
