# fs-adsync — задачи по этапам

Разбивка по этапам ведётся здесь перед началом каждого этапа: какие файлы меняются, какие
классы/методы добавляются, какими тестами покрываются. Реализация начинается только после
согласования разбивки с пользователем (см. `.docs/CLAUDE.md` — «работать поэтапно»).

---

## Этап 0 — Скаффолд (сделано, раскладка уточнена)

Инструментальный каркас: `pyproject.toml` (uv, ruff, mypy strict, pytest), `.env.example`,
`config/subjects.yaml.example`, `.gitignore`, `.github/workflows/ci.yml`. Без файлов-заглушек
будущих модулей — см. память проекта о правке подхода.

**Раскладка изменена по просьбе пользователя**: без пакета `adsync/` внутри `src/` — модули лежат
прямо в `src/*.py` (`src/config.py`, `src/models.py`, …), без `__init__.py`. `src` подключается
через `mypy_path = "src"` и `pytest` `pythonpath = ["src"]`, а не через установку пакета.
`[tool.uv] package = false` — uv только синкает зависимости в venv, ничего не собирает/не
устанавливает как пакет.

**Известное отклонение от текста CLAUDE.md**: раздел Commands называет команду запуска
`uv run adsync` (console-script entry point) — без пакета такой entry point недостижим. Запуск на
этапе 9 будет `uv run python src/main.py` (или аналогичный явный вызов). Отмечено, сам CLAUDE.md
не правим без отдельного запроса.

---

## Этап 1 — Конфиг и модели

### Файлы

| Файл | Статус | Роль |
|---|---|---|
| `src/models.py` | новый | DTO заданий WP + ack, discriminated union по `event` |
| `src/config.py` | новый | `Settings` (pydantic-settings) + загрузка/валидация `subjects.yaml` |
| `tests/test_models.py` | новый | покрытие моделей |
| `tests/test_config.py` | новый | покрытие `Settings` и `load_subjects` |

### `src/models.py`

DTO входящих/исходящих данных WP-контракта (`FS_LMS_API.md` §3), только pydantic-модели, без
логики обработки.

- `class ProvisionJob(BaseModel)` — `id: int`, `event: Literal["provision"]`, `idempotency_key: str`,
  `username: str`, `password: str`, `first: str`, `last: str`, `subject_key: str`.
- `class PromoteJob(BaseModel)` — `id: int`, `event: Literal["promote"]`, `idempotency_key: str`,
  `username: str`.
- `class DeprovisionJob(BaseModel)` — `id: int`, `event: Literal["deprovision"]`,
  `idempotency_key: str`, `username: str`.
- `Job = Annotated[ProvisionJob | PromoteJob | DeprovisionJob, Field(discriminator="event")]` —
  discriminated union.
- `class JobsResponse(BaseModel)` — `jobs: list[Job]`; тело ответа `GET /ad/jobs`.
- `class AckRequest(BaseModel)` — `id: int`, `status: Literal["done", "failed"]`,
  `error: str | None = None`; тело `POST /ad/ack` (без `sam_account_name` — WP его не использует,
  по CLAUDE.md не отправляем).
- `class ActiveUsernamesResponse(BaseModel)` — `usernames: list[str]`; тело
  `GET /ad/active-usernames`.

Все модели `model_config = ConfigDict(extra="forbid")` не ставим (WP может добавлять поля вперёд
совместимости не гарантировано, но лишние поля не критичны) — оставляем `extra="ignore"` (default
pydantic v2), обсудим при первом реальном расхождении.

### `src/config.py`

- `class SubjectConfig(BaseModel)` — `ou_dn: str`, `group_dn: str`, оба с `min_length=1` (непустые
  DN, как того требует CLAUDE.md).
- `class SubjectsFile(BaseModel)` — `subjects: dict[str, SubjectConfig]`; корневая обёртка под
  ключ `subjects:` из yaml.
- `class SubjectsConfigError(ValueError)` — единая ошибка загрузки карты направлений с понятным
  сообщением (файл не найден / битый YAML / невалидная схема / пустой DN).
- `def load_subjects(path: Path) -> dict[str, SubjectConfig]` — читает YAML по `path`
  (`yaml.safe_load`), валидирует через `SubjectsFile`, оборачивает ошибки чтения/парсинга/валидации
  в `SubjectsConfigError` с сообщением, включающим путь к файлу. Не содержит доступа к AD
  (проверка существования DN в AD — это `AdGateway`, этап 4).
- `class Settings(BaseSettings)` — поля по таблице Configuration CLAUDE.md, чтение из `.env`
  (`model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")`):
  - `lms_base_url: str`, `fs_lms_ad_hmac_secret: str` — обязательные, без дефолта.
  - `jobs_poll_seconds: int = 3`, `jobs_limit: int = Field(50, ge=1, le=100)`.
  - `reconcile_interval_hours: int = 6`, `reconcile_grace_minutes: int = 15`,
    `reconcile_max_disable: int = 10`, `reconcile_max_disable_pct: int = Field(20, ge=0, le=100)`.
  - `ldap_host: str = "11.11.11.11"`, `ldap_port: int = 636`,
    `ldap_ca_cert: Path = Path("/app/config/ldap-ca.pem")`,
    `ldap_bind_dn: str`, `ldap_bind_password: str` — обязательные.
  - `ad_upn_suffix: str = "fs.loc"`, `ad_ou_disabled: str`, `ad_ou_fallback: str` — обязательные.
  - `subjects_file: Path = Path("/app/config/subjects.yaml")`, `data_dir: Path = Path("/data")`.
  - `tz_name: str = "Europe/Moscow"`, `daily_summary_time: str | None = None`.
  - `loki_url: str | None = None`, `telegram_bot_token: str | None = None`,
    `telegram_chat_id: str | None = None`.
  - `api_port: int = 8091`.
  - Имена полей в `snake_case` — pydantic-settings сопоставляет их с переменными окружения в
    `UPPER_SNAKE_CASE` без доп. алиасов (регистронезависимо).

### Тесты

`tests/test_models.py`:
- парсинг `ProvisionJob`/`PromoteJob`/`DeprovisionJob` из «сырых» dict-payload (примеры из
  `FS_LMS_API.md` §3.1);
- `JobsResponse` корректно разруливает смешанный список всех трёх типов по `event`
  (discriminated union);
- неизвестный `event` → `ValidationError`;
- отсутствие обязательного поля (например, `password` у `provision`) → `ValidationError`;
- `AckRequest`: `status="done"` и `status="failed", error="..."` валидны; произвольная строка в
  `status` (не `done`/`failed`) → `ValidationError`;
- `ActiveUsernamesResponse` парсит `{"usernames": [...]}`.

`tests/test_config.py`:
- `Settings` собирается из переменных окружения (monkeypatch `os.environ`), дефолты применяются
  там, где env не задан;
- отсутствие обязательной переменной (например, `LMS_BASE_URL`) → `ValidationError` при создании
  `Settings`;
- `load_subjects` на валидном yaml (по образцу `config/subjects.yaml.example`) возвращает
  `dict[str, SubjectConfig]` с ожидаемыми `ou_dn`/`group_dn`;
- `load_subjects` на несуществующем пути → `SubjectsConfigError`;
- `load_subjects` на yaml с пустым `ou_dn`/`group_dn` → `SubjectsConfigError`;
- `load_subjects` на yaml без ключа `subjects` / с неверной схемой → `SubjectsConfigError`.

### Проверка перед завершением этапа

```
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

Зависимости новые не нужны — `pydantic`, `pydantic-settings`, `PyYAML` уже добавлены на этапе 0.

---

## Этап 2 — Журнал (SQLite)

### Файлы

| Файл | Статус | Роль |
|---|---|---|
| `src/repository.py` | новый | Append-only журнал обработок в SQLite (WAL), dead-счётчик |
| `tests/test_repository.py` | новый | покрытие репозитория |

### `src/repository.py`

Единственная точка доступа к `DATA_DIR/state.db`; без ORM, `sqlite3` (stdlib) напрямую. Не знает
про ack/HTTP/бизнес-правила — только хранит факты и умеет их агрегировать.

- `@dataclass(frozen=True, slots=True) class JobLogEntry` — внутренний value-object одной строки
  журнала: `job_id: int`, `idempotency_key: str`, `event: str`, `username: str`,
  `subject_key: str | None`, `status: Literal["done", "failed"]`, `error: str | None`,
  `received_at: datetime`, `acked_at: datetime`. Вызывающий код (poller, этап 6) передаёт уже
  готовые UTC-datetime — репозиторий не работает со временем сам по себе (никакого `now`
  внутри), только форматирует в ISO 8601 при записи.
- `class JobRepository`:
  - `__init__(self, db_path: Path) -> None` — создаёт `db_path.parent`, если его нет; открывает
    `sqlite3.connect(db_path, check_same_thread=False)` (доступ из разных потоков: poller,
    reconcile, локальное API); включает `PRAGMA journal_mode=WAL`; создаёт таблицу `jobs` и индекс
    по `idempotency_key`, если их ещё нет. Внутренний `threading.Lock` сериализует запись/чтение
    (sqlite3-соединение не потокобезопасно для конкурентного использования).
  - `def record(self, entry: JobLogEntry) -> None` — **append-only** `INSERT` строки журнала.
    Никогда не апдейтит и не удаляет существующие строки — повторная выдача одного и того же
    `idempotency_key` пишет новую строку, а не перезаписывает старую (это и есть источник
    dead-счётчика).
  - `def dead_count(self, idempotency_key: str) -> int` — `SELECT COUNT(*) FROM jobs WHERE
    idempotency_key = ? AND status = 'failed'`.
  - `def close(self) -> None` — закрывает соединение (для graceful shutdown в `main.py`, этап 9).

Схема таблицы `jobs` — ровно по разделу State (SQLite) CLAUDE.md: `id INTEGER PRIMARY KEY
AUTOINCREMENT`, `job_id`, `idempotency_key`, `event`, `username`, `subject_key` (nullable),
`status` (`CHECK IN ('done','failed')`), `error` (nullable), `received_at`, `acked_at` (оба —
`TEXT`, UTC ISO 8601). **Пароль и сырой payload в схеме отсутствуют в принципе** — ни колонки,
ни возможности их туда положить.

Не добавляется на этом этапе (будет добавлено, когда реально понадобится соответствующему
потребителю): выборка последних N записей для `/status` (этап 9), агрегаты для дневной сводки
(этап 8) — тонкие read-методы допишем в те этапы, а не заранее.

### Тесты (`tests/test_repository.py`, всё на `tmp_path`)

- запись через `record()` появляется в БД с ожидаемыми полями (читаем сырым `sqlite3.connect` —
  без обхода через репозиторий, чтобы не тестировать «через себя»);
- в схеме таблицы нет колонки под пароль/сырой payload (`PRAGMA table_info(jobs)` не содержит
  `password`/`payload`);
- **append-only**: два вызова `record()` с одинаковым `idempotency_key` дают две строки, а не одну
  перезаписанную;
- `dead_count()` считает только `status='failed'` по конкретному `idempotency_key`, не задевая
  `done`-записи и записи с другим ключом;
- журнал открыт в режиме WAL (`PRAGMA journal_mode` → `wal`);
- `db_path` с несуществующей родительской директорией — `JobRepository` создаёт её сама;
- `close()` не бросает исключений и корректно освобождает соединение.

### Проверка перед завершением этапа

```
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

Зависимости новые не нужны — `sqlite3`/`threading`/`dataclasses` — stdlib.
