"""In-memory фейк `DirectoryGateway` для тестов (без сети, без реального AD)."""

from datetime import UTC, datetime

from ad import DirectoryUnavailableError, DirectoryUser, OutsideManagedZoneError, ZoneAccount

_DEFAULT_CREATED_AT = datetime(2000, 1, 1, tzinfo=UTC)


class FakeDirectoryGateway:
    """Реализация `DirectoryGateway` в памяти: словари вместо реального AD."""

    def __init__(
        self,
        *,
        zone_ou_dns: set[str] | None = None,
        ou_disabled: str = "",
        ou_fallback: str = "",
    ) -> None:
        self._zone_dns = (zone_ou_dns or set()) | {ou_disabled, ou_fallback}
        self._ou_disabled = ou_disabled
        self._ou_fallback = ou_fallback
        self.users_by_username: dict[str, DirectoryUser] = {}
        self.group_members: dict[str, set[str]] = {}
        self.passwords: dict[str, str] = {}
        self.created_at: dict[str, datetime] = {}
        # True — имитация недоступного DC: чтение из AD поднимает DirectoryUnavailableError.
        self.unavailable = False

    def _check_available(self) -> None:
        if self.unavailable:
            raise DirectoryUnavailableError("fake DC is down")

    def ping(self) -> None:
        self._check_available()

    def is_in_managed_zone(self, dn: str) -> bool:
        return any(dn.endswith(zone_dn) for zone_dn in self._zone_dns if zone_dn)

    def is_in_disabled_ou(self, dn: str) -> bool:
        return bool(self._ou_disabled) and dn.endswith(self._ou_disabled)

    def find_user(self, username: str) -> DirectoryUser | None:
        self._check_available()
        return self.users_by_username.get(username)

    def create_user(
        self,
        *,
        ou_dn: str,
        username: str,
        first: str,
        last: str,
        created_at: datetime | None = None,
    ) -> str:
        dn = f"CN={first} {last},{ou_dn}"
        # Как и настоящий AdGateway: создаём ОТКЛЮЧЁННОЙ — AD не даёт включить учётку без пароля
        # (WILL_NOT_PERFORM). Включение — отдельный вызов ensure_enabled после ensure_password.
        self.users_by_username[username] = DirectoryUser(dn=dn, enabled=False)
        self.created_at[username] = created_at or _DEFAULT_CREATED_AT
        return dn

    def ensure_password(self, dn: str, password: str) -> None:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)
        self.passwords[dn] = password

    def ensure_enabled(self, dn: str) -> None:
        self._set_enabled(dn, enabled=True)

    def ensure_disabled(self, dn: str) -> None:
        self._set_enabled(dn, enabled=False)

    def _set_enabled(self, dn: str, *, enabled: bool) -> None:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)
        for username, user in self.users_by_username.items():
            if user.dn == dn:
                self.users_by_username[username] = DirectoryUser(dn=dn, enabled=enabled)
                return

    def ensure_group_membership(self, user_dn: str, group_dn: str) -> None:
        if not self.is_in_managed_zone(user_dn):
            raise OutsideManagedZoneError(user_dn)
        self.group_members.setdefault(group_dn, set()).add(user_dn)

    def move_to_ou(self, dn: str, target_ou_dn: str) -> str:
        if not self.is_in_managed_zone(dn):
            raise OutsideManagedZoneError(dn)
        rdn = dn.split(",", 1)[0]
        new_dn = f"{rdn},{target_ou_dn}"
        for username, user in self.users_by_username.items():
            if user.dn == dn:
                self.users_by_username[username] = DirectoryUser(dn=new_dn, enabled=user.enabled)
        return new_dn

    def verify_zone_exists(self) -> None:
        return None

    def list_zone_accounts(self) -> list[ZoneAccount]:
        self._check_available()
        return [
            ZoneAccount(
                dn=user.dn,
                username=username,
                enabled=user.enabled,
                created_at=self.created_at[username],
            )
            for username, user in self.users_by_username.items()
            if self.is_in_managed_zone(user.dn) and not self.is_in_disabled_ou(user.dn)
        ]
