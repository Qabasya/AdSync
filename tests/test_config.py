"""Тесты `Settings` и `load_subjects` (`config.py`)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Settings, SubjectsConfigError, load_subjects

REQUIRED_ENV = {
    "LMS_BASE_URL": "https://example.com/wp-json/fs-lms/v1",
    "FS_LMS_AD_HMAC_SECRET": "secret",
    "LDAP_BIND_DN": "CN=svc-adsync,OU=Service,DC=fs,DC=loc",
    "LDAP_BIND_PASSWORD": "bind-password",
    "AD_OU_DISABLED": "OU=Отчисленные,DC=fs,DC=loc",
    "AD_OU_FALLBACK": "OU=Без направления,DC=fs,DC=loc",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_settings_loads_from_env_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.lms_base_url == REQUIRED_ENV["LMS_BASE_URL"]
    assert settings.jobs_poll_seconds == 3
    assert settings.jobs_limit == 50
    assert settings.ldap_host == "11.11.11.11"
    assert settings.ldap_port == 636
    assert settings.data_dir == Path("/data")
    assert settings.api_port == 8091
    assert settings.loki_url is None
    assert settings.heartbeat_interval_seconds == 3600


def test_settings_overrides_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("JOBS_POLL_SECONDS", "5")
    monkeypatch.setenv("API_PORT", "9000")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.jobs_poll_seconds == 5
    assert settings.api_port == 9000


def test_settings_rejects_non_positive_heartbeat_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_missing_required_variable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_load_subjects_valid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "subjects.yaml"
    path.write_text(
        """
subjects:
  inf-ege:
    ou_dn: "OU=КЕГЭ,OU=Ученики,DC=fs,DC=loc"
    group_dn: "CN=KEGE,OU=Группы,DC=fs,DC=loc"
""",
        encoding="utf-8",
    )

    subjects = load_subjects(path)

    assert subjects["inf-ege"].ou_dn == "OU=КЕГЭ,OU=Ученики,DC=fs,DC=loc"
    assert subjects["inf-ege"].group_dn == "CN=KEGE,OU=Группы,DC=fs,DC=loc"


def test_load_subjects_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SubjectsConfigError):
        load_subjects(tmp_path / "does-not-exist.yaml")


def test_load_subjects_empty_dn_raises(tmp_path: Path) -> None:
    path = tmp_path / "subjects.yaml"
    path.write_text(
        """
subjects:
  inf-ege:
    ou_dn: ""
    group_dn: "CN=KEGE,OU=Группы,DC=fs,DC=loc"
""",
        encoding="utf-8",
    )

    with pytest.raises(SubjectsConfigError):
        load_subjects(path)


def test_load_subjects_malformed_schema_raises(tmp_path: Path) -> None:
    path = tmp_path / "subjects.yaml"
    path.write_text('subjects:\n  inf-ege:\n    ou_dn: "OU=X,DC=fs,DC=loc"\n', encoding="utf-8")

    with pytest.raises(SubjectsConfigError):
        load_subjects(path)
