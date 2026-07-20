"""Настройки сервиса (``Settings``) и загрузка карты направлений (``subjects.yaml``)."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class SubjectConfig(BaseModel):
    """DN OU и security-группы одного направления из ``subjects.yaml``."""

    ou_dn: str = Field(min_length=1)
    group_dn: str = Field(min_length=1)


class SubjectsFile(BaseModel):
    """Корневая схема ``subjects.yaml``: карта ``subject_key -> SubjectConfig``."""

    subjects: dict[str, SubjectConfig]


class SubjectsConfigError(ValueError):
    """Ошибка чтения или валидации ``subjects.yaml`` — падение на старте с понятным сообщением."""


def load_subjects(path: Path) -> dict[str, SubjectConfig]:
    """Читает и валидирует карту направлений.

    Args:
        path: путь к yaml-файлу (см. ``config/subjects.yaml.example``).

    Returns:
        Карта ``subject_key -> SubjectConfig``.

    Raises:
        SubjectsConfigError: файл не найден, битый YAML или схема не соответствует ожидаемой.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SubjectsConfigError(f"не удалось прочитать {path}: {exc}") from exc

    try:
        raw_data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SubjectsConfigError(f"невалидный YAML в {path}: {exc}") from exc

    try:
        parsed = SubjectsFile.model_validate(raw_data)
    except ValidationError as exc:
        raise SubjectsConfigError(f"невалидная схема {path}: {exc}") from exc

    return parsed.subjects


class Settings(BaseSettings):
    """Конфигурация сервиса из переменных окружения / ``.env`` (см. таблицу Configuration)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    lms_base_url: str
    fs_lms_ad_hmac_secret: str

    jobs_poll_seconds: int = 3
    jobs_limit: int = Field(default=50, ge=1, le=200)

    reconcile_interval_hours: int = 6
    reconcile_grace_minutes: int = 15
    reconcile_max_disable: int = 10
    reconcile_max_disable_pct: int = Field(default=20, ge=0, le=100)

    ldap_host: str = "11.11.11.11"
    ldap_port: int = 636
    ldap_ca_cert: Path = Path("/app/config/ldap-ca.pem")
    ldap_bind_dn: str
    ldap_bind_password: str

    ad_upn_suffix: str = "fs.loc"
    ad_ou_disabled: str
    ad_ou_fallback: str

    subjects_file: Path = Path("/app/config/subjects.yaml")
    data_dir: Path = Path("/data")

    tz_name: str = "Europe/Moscow"
    daily_summary_time: str | None = None

    loki_url: str | None = None

    api_port: int = 8091
