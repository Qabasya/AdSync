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

---

## Этап 3 — LMS-клиент

### Файлы

| Файл | Статус | Роль |
|---|---|---|
| `src/lms.py` | новый | `LmsApi` (Protocol) + `LmsClient` (httpx): `get_jobs`/`ack`/`get_active_usernames`, HMAC-подпись |
| `tests/fakes.py` | новый | `FakeLmsApi` — in-memory реализация `LmsApi` для тестов будущих этапов (poller, handlers, reconcile) |
| `tests/test_lms.py` | новый | покрытие `LmsClient`: подпись, парсинг, отсутствие ретраев |

### `src/lms.py`

- `class LmsApi(Protocol)`:
  - `def get_jobs(self, limit: int) -> list[Job]: ...`
  - `def ack(self, request: AckRequest) -> None: ...`
  - `def get_active_usernames(self) -> list[str]: ...`
- `class LmsClient`:
  - `__init__(self, base_url: str, hmac_secret: str, *, timeout: float = 15.0, transport:
    httpx.BaseTransport | None = None) -> None` — держит `httpx.Client(base_url=..., timeout=...,
    transport=...)`. Параметр `transport` — исключительно для тестов (внедрение
    `httpx.MockTransport`, без реальной сети); в проде остаётся `None` (обычный сетевой transport).
  - `_signed_headers(self, body: str) -> dict[str, str]` — точная формула `FS_LMS_API.md` §2:
    `ts = str(int(time.time()))`, `sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(),
    hashlib.sha256).hexdigest()`, заголовки `X-Fs-Timestamp`/`X-Fs-Signature`. Только stdlib
    `time`/`hmac`/`hashlib`.
  - `get_jobs(self, limit: int) -> list[Job]` — `GET /ad/jobs?limit=…` с подписью по пустому телу
    (`""` для GET), `raise_for_status()`, парсинг тела через `JobsResponse.model_validate(...).jobs`.
  - `ack(self, request: AckRequest) -> None` — `POST /ad/ack`; тело —
    `request.model_dump_json(exclude_none=True)` (если `error is None`, поле в JSON не отправляется;
    `sam_account_name` не отправляем — его нет в `AckRequest`); подпись считается **по тем же самым
    байтам**, что реально уходят в теле запроса; `raise_for_status()`.
  - `get_active_usernames(self) -> list[str]` — `GET /ad/active-usernames`, отдельный больший
    таймаут (`timeout=30.0` на этот конкретный запрос — список может быть большим), парсинг через
    `ActiveUsernamesResponse.model_validate(...).usernames`.
  - `close(self) -> None` — закрывает `httpx.Client`.
  - **Без собственных ретраев**: любая сетевая ошибка/`4xx`/`5xx` просто пробрасывается наружу
    (`httpx.HTTPError` и наследники) — решение, что делать дальше (следующий тик / `ack(failed)`),
    принимает вызывающий код (`poller.py`, этап 6), не `lms.py`.

### `tests/fakes.py`

- `class FakeLmsApi` — реализует `LmsApi` без сети: конструктор принимает начальный список
  `jobs: list[Job]` и `active_usernames: list[str]`; `get_jobs(limit)` возвращает срез списка;
  `ack(request)` копит вызовы в `self.acks: list[AckRequest]` (для проверки в тестах будущих
  этапов, что и с каким статусом было отправлено); `get_active_usernames()` возвращает список как
  есть. Без задержек, без ошибок по умолчанию — усложнения (эмуляция сетевых сбоев и т.п.)
  добавляются в тот этап, где они реально понадобятся конкретному тесту.

### Тесты (`tests/test_lms.py`, сеть запрещена — `httpx.MockTransport` вместо реальных запросов)

- `get_jobs`: запрос уходит на `GET /ad/jobs` с `limit` в query, тело для подписи — пустая строка;
  заголовок `X-Fs-Signature` **пересчитывается независимо** в тесте по формуле из
  `FS_LMS_API.md` §2 (тот же `hmac.new(secret, f"{ts}.{body}", sha256).hexdigest()`, где `ts` берём
  из фактически отправленного `X-Fs-Timestamp`) и сверяется с тем, что реально отправил клиент —
  это и есть «вектор» подписи; смешанный ответ (`provision`/`promote`/`deprovision`) корректно
  парсится в `list[Job]`.
- `ack`: тело запроса — валидный JSON с `id`/`status` и без `sam_account_name`; при `error=None`
  поле `error` в теле отсутствует, при заданном `error` — присутствует; подпись пересчитывается по
  **фактически отправленным байтам тела** и совпадает с заголовком.
- `get_active_usernames`: `GET /ad/active-usernames`, тело для подписи пустое, ответ
  `{"usernames": [...]}` парсится в `list[str]`.
- `5xx`/`4xx` ответ от `GET /ad/jobs` → `LmsClient` не глотает и не ретраит, исключение
  (`httpx.HTTPStatusError`) пробрасывается наружу.

### Проверка перед завершением этапа

```
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

Зависимости новые не нужны — `httpx` уже добавлен на этапе 0; `httpx.MockTransport` — часть самого
`httpx`, отдельного тестового HTTP-мока (`respx`, `pytest-httpx` и т.п.) не добавляем.

---

## Этап 4 — AD-шлюз

Разведка перед проектированием (сделана): `ldap3` (2.9.1) поддерживает тестовую стратегию
`MOCK_SYNC` — полноценная in-memory директория без сети и без сторонних зависимостей:
`Connection(server, ..., client_strategy=MOCK_SYNC)`, `connection.strategy.add_entry(dn, attrs)`
для затравки, дальше обычные `search`/`add`/`modify`/`modify_dn`/
`extend.microsoft.modify_password` работают как на настоящем сервере и бросают те же исключения
(`LDAPEntryAlreadyExistsResult`, `LDAPNoSuchObjectResult` и т.д. при `raise_exceptions=True`).
Этим и пользуемся в `tests/test_ad.py` — реальный `AdGateway`, без сети.

Важно: `ad.py` реализует только **примитивы** (по одному LDAP-действию на метод) + zone-guard +
переподключение. Ветвление по бизнес-правилам таблицы «События → действия в AD» и «Правила поверх
таблицы» (что делать, если учётка уже существует / в «Отчисленных» / вне зоны и т.д.) — это
`handlers.py`, этап 5. `ad.py` про них не знает.

### Файлы

| Файл | Статус | Роль |
|---|---|---|
| `src/ad.py` | новый | `DirectoryGateway` (Protocol) + `AdGateway` (ldap3): примитивы, zone-guard, реконнект |
| `tests/fakes.py` | правка | добавить `FakeDirectoryGateway` |
| `tests/test_ad.py` | новый | покрытие `AdGateway` на `MOCK_SYNC` |

### `src/ad.py`

- `@dataclass(frozen=True, slots=True) class DirectoryUser` — `dn: str`, `enabled: bool`; то, что
  возвращает поиск учётки.
- `class OutsideManagedZoneError(Exception)` — попытка операции над DN вне управляемой зоны;
  объект не трогается.
- `class ManagedZoneConfigError(Exception)` — на старте не найден в AD один из DN конфигурации
  (`ou_dn`/`group_dn` из `subjects.yaml`, `AD_OU_DISABLED`, `AD_OU_FALLBACK`).
- `class DirectoryGateway(Protocol)`:
  - `find_user(self, username: str) -> DirectoryUser | None` — поиск по `sAMAccountName` от
    корня домена (не только в зоне — иначе не отличить «чужую» учётку от отсутствующей).
  - `is_in_managed_zone(self, dn: str) -> bool` / `is_in_disabled_ou(self, dn: str) -> bool`.
  - `create_user(self, *, ou_dn: str, username: str, first: str, last: str) -> str` — DN.
  - `ensure_password(self, dn: str, password: str) -> None`.
  - `ensure_enabled(self, dn: str) -> None` / `ensure_disabled(self, dn: str) -> None`.
  - `ensure_group_membership(self, user_dn: str, group_dn: str) -> None`.
  - `move_to_ou(self, dn: str, target_ou_dn: str) -> str` — новый DN.
  - `verify_zone_exists(self) -> None` — стартовая проверка всех DN конфигурации.
- `class AdGateway`:
  - `__init__(self, connection: ldap3.Connection, *, reconnect: Callable[[], ldap3.Connection],
    subjects: dict[str, SubjectConfig], ou_disabled: str, ou_fallback: str, upn_suffix: str) ->
    None` — конструктор принимает уже готовое (собранное вызывающим кодом) `Connection`;
    `reconnect` — тот же идиом, что `now: Callable[[]]` в проекте: зависимость «дай мне свежее
    соединение», а не «как его строить». `subjects`/`ou_disabled`/`ou_fallback` формируют список DN
    управляемой зоны; `upn_suffix` — для `userPrincipalName` и вычисления корня поиска домена
    (`DC=fs,DC=loc` из `fs.loc`).
  - `_run(self, operation: Callable[[], T]) -> T` — вызывает `operation()`; при
    `LDAPCommunicationError` (родитель `LDAPSocketOpenError`/`LDAPSessionTerminatedByServerError`/
    `LDAPSocketReceiveError` и т.п.) логирует WARNING, вызывает `self._connection =
    self._reconnect()` и повторяет **один раз**. `operation` всегда замыкание над `self`
    (`lambda: self._connection.search(...)`), поэтому повтор идёт уже на новом соединении.
  - `find_user` — `search` от `DC=...` (из `upn_suffix`) по `(sAMAccountName=...)` (значение через
    `ldap3.utils.conv.escape_filter_chars`); `None`, если пусто; иначе `DirectoryUser(dn=entry_dn,
    enabled=not (int(userAccountControl) & 0x2))`.
  - `create_user` — строит `dn = f"CN={escape_rdn(first+' '+last)},{ou_dn}"`,
    `add(dn, ["top","person","organizationalPerson","user"], {...,
    "userAccountControl": 512})`; ловит `LDAPEntryAlreadyExistsResult` — идемпотентно, не ошибка.
    Пароль внутри `create_user` **не** ставится — это отдельный шаг (`ensure_password`), как и в
    таблице CLAUDE.md.
  - `ensure_password` — guard зоны, затем `connection.extend.microsoft.modify_password(dn,
    password)` (ровно то, что требует CLAUDE.md; никакого ручного `modify` по `unicodePwd`).
  - `ensure_enabled`/`ensure_disabled` — guard зоны, `modify(dn, {"userAccountControl":
    [(MODIFY_REPLACE, [512 или 514])]})`.
  - `ensure_group_membership` — guard зоны; сначала `search(group_dn, BASE, attributes=["member"])`
    и проверка, есть ли уже `user_dn` среди значений (`_dn_equals`, регистронезависимое посегментное
    сравнение через `ldap3.utils.dn.parse_dn`); если да — no-op; иначе `MODIFY_ADD`. Идемпотентность
    через предварительную проверку, а не через отлов ошибки «уже есть» (в MOCK_SYNC она не
    воспроизводится, а на реальном AD поведение по этой же причине надёжнее не полагаться на код
    ошибки).
  - `move_to_ou` — guard зоны; если текущий родитель DN (через `parse_dn`) уже равен целевой OU —
    no-op, вернуть тот же DN; иначе `modify_dn(dn, new_rdn, new_superior=target_ou_dn)`.
  - `verify_zone_exists` — по каждому DN из `{ou_dn, group_dn}` всех направлений + `ou_disabled` +
    `ou_fallback` делает `search(dn, BASE, "(objectClass=*)")`; ловит `LDAPNoSuchObjectResult` →
    считает отсутствующим; в конце, если есть отсутствующие — одно
    `ManagedZoneConfigError` со списком всех недостающих DN сразу (не только первого).
  - `close(self) -> None` — `connection.unbind()`.
  - Внутренние хелперы: `_dn_components`/`_is_within_ou`/`_dn_equals`/`_parent_ou` — сравнение DN
    по сегментам через `ldap3.utils.dn.parse_dn` (регистронезависимо), а не наивным `str.endswith`.
- `def build_ldaps_connection(*, host: str, port: int, ca_cert_path: Path, bind_dn: str,
  bind_password: str) -> ldap3.Connection` — модульная функция (не метод): `Tls(validate=
  ssl.CERT_REQUIRED, ca_certs_file=str(ca_cert_path))` → `Server(host, port=port, use_ssl=True,
  tls=tls)` → `Connection(server, user=bind_dn, password=bind_password, auto_bind=True,
  raise_exceptions=True)`. Используется `main.py` (этап 9) для сборки боевого соединения и как
  `reconnect`-замыкание. **Не покрывается pytest** (нужен реальный DC с LDAPS) — проверяется
  `scripts/smoke.py` (этап 10), как и оговорено в разделе Testing CLAUDE.md.

### `tests/fakes.py` — добавить `FakeDirectoryGateway`

In-memory реализация `DirectoryGateway` для будущих тестов `handlers.py`/`poller.py`/
`reconcile.py`: словари `username -> DirectoryUser`, `group_dn -> set[user_dn]`; `create_user`/
`ensure_password`/`ensure_enabled`/`ensure_disabled`/`ensure_group_membership`/`move_to_ou` мутируют
эти словари; `is_in_managed_zone`/`is_in_disabled_ou` — сравнение по сконфигурированным на
конструкторе множествам OU; `verify_zone_exists` — no-op. Без сети, без ldap3.

### Тесты (`tests/test_ad.py`, на реальном `AdGateway` + `ldap3` `MOCK_SYNC`, сеть не участвует)

- `find_user`: `None`, если не найдена; `DirectoryUser(dn, enabled)` — корректный `enabled` для
  `userAccountControl=512` и `=514`.
- `create_user` идемпотентен: два вызова с той же учёткой — не бросает, оба раза один и тот же DN.
- `ensure_password` не бросает на существующей в зоне учётке (сам факт вызова
  `extend.microsoft.modify_password` через `MOCK_SYNC` проходит).
- `ensure_enabled`/`ensure_disabled` меняют видимое через `find_user` состояние `enabled`.
- `ensure_group_membership`: после вызова пользователь — член группы; повторный вызов не создаёт
  дубликат в `member`.
- `move_to_ou`: DN учётки меняется на ожидаемый; повторный вызов в ту же OU — не бросает, возвращает
  тот же DN.
- **Zone guard**: `ensure_password`/`ensure_enabled`/`ensure_group_membership`/`move_to_ou` на DN
  вне сконфигурированной зоны → `OutsideManagedZoneError`, объект в директории не изменяется.
- `is_in_managed_zone`/`is_in_disabled_ou`: корректная классификация DN из зоны/«Отчисленных»/чужого.
- `verify_zone_exists`: проходит, если все DN конфигурации существуют в директории; бросает
  `ManagedZoneConfigError` со **списком всех** недостающих DN, если каких-то нет.
- **Реконнект**: тестовая обёртка над `Connection`, у которой первый вызов выбранного метода бросает
  `LDAPSocketOpenError`, второй — работает; `reconnect=` возвращает новое рабочее соединение;
  проверяем, что операция в итоге отработала (используя новое соединение), а не упала и не зациклилась.

### Проверка перед завершением этапа

```
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

Зависимости новые не нужны — `ldap3` добавлен на этапе 0; `MOCK_SYNC` — часть самого `ldap3`.

---

## Этап 5 — Обработчики заданий

`handlers.py` реализует бизнес-ветвление таблицы «События → действия в AD» и «Правила поверх
таблицы» из `.docs/CLAUDE.md`, опираясь только на примитивы `DirectoryGateway` (`ad.py`, этап 4).
Про HTTP/ack/журнал не знает — это `poller.py` (этап 6): он же ловит любые исключения из
`handle()` (LDAP-ошибки, `OutsideManagedZoneError` и т.п.) и превращает их в `ack(failed)`.
Обработчики сами намеренно **предотвращают** `OutsideManagedZoneError`, заранее проверяя зону —
исключение из `AdGateway` тут означает баг в самом обработчике, а не штатный путь.

### Файлы

| Файл | Статус | Роль |
|---|---|---|
| `src/handlers.py` | новый | `JobHandler` (Protocol) + `HandlerResult` + `ProvisionHandler` / `PromoteHandler` / `DeprovisionHandler` |
| `tests/test_handlers.py` | новый | покрытие всех трёх обработчиков по правилам CLAUDE.md |

`tests/fakes.py` не меняется — `FakeDirectoryGateway` уже умеет всё нужное (создание в
произвольной OU без zone-guard на `create_user`, идемпотентный `ensure_group_membership` через
`set`, зона считается по DN).

### `src/handlers.py`

- `@dataclass(frozen=True, slots=True) class HandlerResult` — `status: Literal["done", "failed"]`,
  `error: str | None = None`. Возвращается `handle()`; `poller.py` строит из него `AckRequest` и
  (вместе с полями исходного `job`) `JobLogEntry` — `handlers.py` про журнал и ack не знает.
- `class JobHandler(Protocol)`: `def handle(self, job: Job) -> HandlerResult: ...`. Диспетчеризация
  по `job.event` — реестр `dict[str, JobHandler]` в `main.py` (этап 9); каждый конкретный
  обработчик получает через реестр только «свой» тип задания, поэтому внутри `handle()` —
  `assert isinstance(job, ProvisionJob)` (аналогично для двух других) для сужения типа под mypy
  strict.
- `class ProvisionHandler`:
  - `__init__(self, directory: DirectoryGateway, *, subjects: dict[str, SubjectConfig],
    ou_fallback: str) -> None`.
  - `handle(self, job: ProvisionJob) -> HandlerResult`:
    1. Резолвит цель: `subject = subjects.get(job.subject_key)`; если найден —
       `target_ou, target_group = subject.ou_dn, subject.group_dn`; если нет —
       `target_ou, target_group = ou_fallback, None` + `logger.warning(...)` c `username` и
       `subject_key` (уведомление `Notifier.unknown_subject` — хук добавится, когда появится
       `notifier.py`; пока фиксируем только через лог, как и требует раздел Logging CLAUDE.md
       минимально).
    2. `existing = directory.find_user(job.username)`.
    3. Нет учётки → `directory.create_user(ou_dn=target_ou, username=job.username, first=job.first,
       last=job.last)` → `ensure_password(dn, job.password)` → если `target_group` задан —
       `ensure_group_membership(dn, target_group)` → `HandlerResult("done")`.
    4. Есть учётка, `directory.is_in_disabled_ou(existing.dn)` → реактивация в порядке из
       CLAUDE.md: `ensure_enabled(dn)` → `new_dn = move_to_ou(dn, target_ou)` →
       `ensure_password(new_dn, job.password)` → `ensure_group_membership(new_dn, target_group)`
       если задан → `HandlerResult("done")`.
    5. Есть учётка, `directory.is_in_managed_zone(existing.dn)` (в зоне, не «Отчисленные») →
       `ensure_password(dn, job.password)` → `ensure_group_membership(dn, target_group)` если
       задан → `HandlerResult("done")` (без переноса OU — учётка остаётся, где была).
    6. Есть учётка вне управляемой зоны → **не трогать объект**, `logger.error(...)` →
       `HandlerResult("failed", error="учётная запись вне управляемой зоны")`.
- `class PromoteHandler`:
  - `__init__(self, directory: DirectoryGateway) -> None`.
  - `handle(self, job: PromoteJob) -> HandlerResult`: `user = directory.find_user(job.username)`;
    `None` → `HandlerResult("failed", error="учётная запись не найдена")`; иначе — `done`, если
    `user.enabled and directory.is_in_managed_zone(user.dn)`, иначе `HandlerResult("failed",
    error="учётная запись отключена или вне управляемой зоны")`.
- `class DeprovisionHandler`:
  - `__init__(self, directory: DirectoryGateway, *, ou_disabled: str) -> None`.
  - `handle(self, job: DeprovisionJob) -> HandlerResult`:
    - `user = directory.find_user(job.username)`; нет учётки → `HandlerResult("done")` (цель уже
      достигнута).
    - Есть учётка вне управляемой зоны → не трогать, `logger.error(...)` →
      `HandlerResult("failed", error="учётная запись вне управляемой зоны")`.
    - Есть учётка, `not user.enabled` → уже отключена → `HandlerResult("done")` без действий.
    - Иначе → `directory.ensure_disabled(user.dn)` → `directory.move_to_ou(user.dn, ou_disabled)` →
      `HandlerResult("done")`.
- Логгер модуля: `logging.getLogger("adsync.handlers")`.

### Тесты (`tests/test_handlers.py`, на `FakeDirectoryGateway`, без сети/LDAP)

`ProvisionHandler`:
- новая учётка, известный `subject_key` → создана в `subject.ou_dn`, пароль и членство в группе
  выставлены, `HandlerResult("done")`;
- новая учётка, неизвестный `subject_key` → создана в `ou_fallback`, без группы, `WARNING` в
  логе (`caplog`), `done`;
- учётка уже существует в OU направления (в зоне, не «Отчисленные») → пароль обновлён, членство в
  группе обеспечено, OU не менялась, `done`;
- учётка в «Отчисленных» → реактивация: включена, перенесена в `target_ou`, пароль и группа
  выставлены, `done`;
- учётка вне управляемой зоны → `failed` с непустым `error`, объект в фейке не изменился (dn,
  enabled, пароль не записан);
- повторная выдача того же задания (идемпотентность) → второй вызов тоже `done`, дублей в
  `group_members` нет (проверка через `set`).

`PromoteHandler`:
- учётки нет → `failed`;
- учётка есть, включена, в зоне → `done`;
- учётка есть, но отключена → `failed`;
- учётка есть, включена, но вне зоны → `failed`.

`DeprovisionHandler`:
- учётки нет → `done`, без побочных эффектов;
- учётка есть, включена, в зоне → отключена и перенесена в `ou_disabled`, `done`;
- учётка уже отключена → `done`, без изменений (позиция и флаг не трогаются повторно);
- учётка есть, вне зоны → `failed`, объект не изменён.

### Проверка перед завершением этапа

```
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

Зависимости новые не нужны.

---

## Этап 6 — Поллер заданий

`poller.py` — цикл заданий: `fetch → dispatch → ack → журнал`, последовательно, один поток
(раздел Job Processing Rules CLAUDE.md). Про AD/LDAP не знает — только `LmsApi`, реестр
`JobHandler` и `JobRepository`. Про потоки/`while not stop.wait(...)`/SIGTERM не знает — это
`main.py` (этап 9); `poller.py` даёт один метод «прогнать один тик».

### Файлы

| Файл | Статус | Роль |
|---|---|---|
| `src/poller.py` | новый | `Poller`: один тик обработки заданий |
| `tests/fakes.py` | правка | `FakeLmsApi` — опциональная имитация сетевых ошибок `get_jobs`/`ack` |
| `tests/test_poller.py` | новый | покрытие `Poller` на `FakeLmsApi` + реальных обработчиках/`FakeDirectoryGateway` + `JobRepository` на `tmp_path` |

### `src/poller.py`

- `class Poller`:
  - `__init__(self, lms: LmsApi, *, handlers: dict[str, JobHandler], repository: JobRepository,
    jobs_limit: int, now: Callable[[], datetime]) -> None` — `now` внедряется зависимостью (как
    того требует раздел Code Style CLAUDE.md), `repository.record()` сам с датой/временем не
    работает (см. `repository.py`, этап 2).
  - `def run_once(self) -> None`:
    1. `received_at = self._now()`; `jobs = self._lms.get_jobs(self._jobs_limit)` — любое
       исключение (сеть, `httpx.HTTPStatusError`, `pydantic.ValidationError` при невалидном
       задании в списке — весь список валидируется атомарно, см. ниже «Известное отклонение») →
       `logger.error(...)`, тик завершается, ack никому не шлётся, цикл не падает.
    2. Для каждого `job` из `jobs` — `self._process(job, received_at)`; ошибка одного задания не
       прерывает обработку остальных.
  - `def _process(self, job: Job, received_at: datetime) -> None`:
    1. `handler = self._handlers.get(job.event)`; если обработчика нет (бага реестра в
       `main.py`) — `HandlerResult("failed", error=f"нет обработчика для {job.event!r}")` без
       попытки вызова.
    2. Иначе — вызов `handler.handle(job)` в `try/except Exception`; исключение (LDAP-ошибка,
       `OutsideManagedZoneError` и т.п.) → `logger.exception(...)` (без вывода самого `job` целиком
       — только `job.id`/`job.event`/`job.username`, пароль в лог никогда не должен попасть) →
       `HandlerResult("failed", error=str(exc))`.
    3. `acked_at = self._now()`; `self._lms.ack(AckRequest(id=job.id, status=result.status,
       error=result.error))` в `try/except Exception` — сбой самого ack (сеть) →
       `logger.error(...)`, **запись в журнал не делается** (WP ack не получил — само задание
       естественным образом переотдастся на следующий опрос, штатный pull-ретрай), выходим без
       падения.
    4. Ack прошёл → `subject_key = job.subject_key if isinstance(job, ProvisionJob) else None` →
       `repository.record(JobLogEntry(job_id=job.id, idempotency_key=job.idempotency_key,
       event=job.event, username=job.username, subject_key=subject_key, status=result.status,
       error=result.error, received_at=received_at, acked_at=acked_at))`.
    5. Если `result.status == "failed"` — `dead = repository.dead_count(job.idempotency_key)`;
       `dead >= 6` (порог из раздела Dead CLAUDE.md) → `logger.error(...)` («задание мертво»).
       Настоящий `Notifier.job_dead(...)` подключится, когда появится `notifier.py` — пока это
       единственный сигнал, аналогично `unknown_subject` в `handlers.py` (этап 5).
  - Логгер модуля: `logging.getLogger("adsync.poller")`.

**Известное отклонение**: правило «невалидное задание → `failed` + ERROR, без попытки обработки»
трактуется на уровне **всего тика**, а не отдельного задания — `JobsResponse.model_validate(...)`
в `lms.py` (этап 3) валидирует список атомарно, отдельное невалидное задание нельзя выделить и
поднять ack по нему без переработки `models.py`/`lms.py` (не входит в этот этап). На практике:
один битый элемент в ответе роняет `get_jobs()` целиком → весь тик логируется как ERROR и штатно
повторяется на следующем цикле, сервис не падает — соответствует духу правила («не пытаться
обработать»), но не даёт ack по конкретному сломанному заданию.

### `tests/fakes.py` — правка `FakeLmsApi`

- Добавить опциональные поля конструктора `get_jobs_error: Exception | None = None`,
  `ack_error: Exception | None = None`; если заданы — соответствующий метод бросает это исключение
  вместо обычной работы. Нужно только сейчас (тесты сетевых сбоев поллера) — до этого фейку это не
  требовалось (см. память проекта об инкрементальном усложнении фейков по мере надобности).

### Тесты (`tests/test_poller.py`)

- Смешанный тик (`provision`/`promote`/`deprovision`) — по каждому уходит `ack` с ожидаемым
  статусом; в журнале (читаем сырым `sqlite3.connect`, как в `test_repository.py`) — по строке на
  задание, `subject_key` заполнен только у `provision`, `NULL` у `promote`/`deprovision`.
  `received_at`/`acked_at` — валидные ISO-таймстемпы из подставленного `now`.
- Обработчик бросает исключение на одном из заданий пачки → `ack(failed, error=...)` по нему,
  журнал получает `status='failed'` с текстом ошибки, **следующее задание пачки всё равно
  обработано** (изоляция ошибок).
- `get_jobs` бросает исключение (`FakeLmsApi(get_jobs_error=...)`) → `run_once()` не падает, `ack`
  не вызывается ни разу, журнал пуст, ошибка залогирована (`caplog`, ERROR).
- `ack` бросает исключение на конкретном задании (`FakeLmsApi(ack_error=...)`) →
  `run_once()` не падает, в журнале для этого задания записи нет (ack не подтверждён), ERROR
  залогирован.
- Dead-порог: журнал предзаполнен 5 записями `status='failed'` с одним `idempotency_key`; тик,
  где обработчик для задания с этим же `idempotency_key` снова падает (6-я неудача) →
  `logger.error` про «мёртвое» задание (`caplog`), обработка при этом не падает.
- Нет обработчика для `job.event` в реестре (пустой `handlers={}`) → `ack(failed, ...)` с понятной
  причиной, журнал получает `status='failed'`, без падения.

### Проверка перед завершением этапа

```
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

Зависимости новые не нужны.
