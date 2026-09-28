# fs-adsync — техническая документация

Сервис, который создаёт и сопровождает доменные учётные записи учеников по заявкам из LMS (WordPress-плагин [fs-lms](https://github.com/Qabasya/fs-lms), модуль `Inc\Modules\AdSync`). WP — публичный сайт вне локальной сети; домен-контроллер — внутри неё. Модель — **push**: сайт сам присылает подписанные задания на белый IP офиса (проброс порта на роутере), сервис выполняет их в Active Directory по LDAPS и в том же ответе возвращает результат. Очередь и ретраи — на стороне сайта. Нормативный контракт — `.docs/AdSyncPythonService.md`.

## 1. Структура проекта

Плоская раскладка (flat layout) — модули лежат прямо в `src/*.py`, без пакета `src/adsync/` и без `__init__.py` (осознанный выбор: `[tool.uv] package = false`, `src` подключается через `mypy_path`/`pytest.pythonpath`, а не через установку пакета). Из-за этого нет консольной команды-скрипта — запуск всегда явный, `python src/main.py`.

```
AdSync/
├── src/
│   ├── main.py            # composition root: Settings → зависимости → два uvicorn + heartbeat/сводка
│   ├── config.py          # Settings (pydantic-settings) + загрузка/валидация subjects.yaml
│   ├── models.py          # ProvisionJob | DeprovisionJob (discriminated union), ответы, запрос сверки
│   │
│   ├── auth.py            # SignatureVerifier: подпись запросов сайта (stdlib hmac), окно времени
│   ├── ad.py              # DirectoryGateway (Protocol) + AdGateway (ldap3): примитивы, zone-guard, реконнект
│   ├── handlers.py        # JobHandler (Protocol) + ProvisionHandler/DeprovisionHandler
│   ├── jobs.py            # JobProcessor: одно задание → обработчик → журнал → итог
│   ├── reconcile.py       # сверка активных учёток зоны против списка от сайта, с предохранителями
│   ├── repository.py      # SQLite-журнал (append-only), dead-счётчик, агрегаты для /status и сводки
│   ├── logging_setup.py   # хендлеры file/Loki + redaction-фильтр пароля
│   └── api.py             # FastAPI: публичный (/v1/*, подпись) и локальный (/health, /status)
│
├── tests/                     # зеркалит src/, фейк AD вместо реального DC (tests/fakes.py)
├── config/subjects.yaml.example    # шаблон карты «направление → OU + группа»
├── Dockerfile, docker-compose.yml
├── .env.example
└── pyproject.toml
```

Входы — только запросы сайта (см. `api.py`):

```
POST /v1/jobs       (сразу после события на сайте): подпись → обработчик → журнал → {status, error}
POST /v1/reconcile  (раз в сутки):  подпись → список «кто должен жить» → отключить лишних (с предохранителями)
GET  /v1/health     (кнопка «Проверить соединение»): подпись → пробный запрос к DC
```

Фоновых циклов опроса нет; остались поток heartbeat и опциональный поток дневной сводки (`DAILY_SUMMARY_TIME`, пусто = выключено).

## 2. Используемые технологии

| Технология | Где используется | Почему |
|---|---|---|
| **Python 3.12+, uv** | весь проект | Единый быстрый резолвер зависимостей, `uv.lock`, одна команда на всё (`uv run`, `uv add`); без `pip`/`poetry`. |
| **pydantic v2 + pydantic-settings** | `config.py` (`Settings`, `SubjectConfig`), `models.py` (discriminated union заданий по полю `event`) | Валидация на старте с понятным сообщением (fail-fast) — не собранный `.env` роняет процесс сразу, а не на первом задании. |
| **httpx** | `logging_setup.py` (push в Loki) | Синхронный клиент с человеческим API; к сайту сервис сам не обращается. |
| **ldap3** | `ad.py` | LDAPS-соединение к AD; установка пароля — только через `extend.microsoft.modify_password`. Тестируется через встроенную in-memory стратегию `MOCK_SYNC` — без сети и без реального DC. |
| **PyYAML** | `config.py` (`load_subjects`) | Единственное место, где сервис читает YAML (`config/subjects.yaml`). |
| **sqlite3 (stdlib)** | `repository.py` | Один файл `state.db`, WAL-режим; журнал обработок — сотни/тысячи строк, полноценная СУБД не нужна. Без ORM. |
| **FastAPI + uvicorn** | `api.py`, `main.py` | Два приложения: публичный API для сайта (TLS, `/v1/jobs`, `/v1/reconcile`, `/v1/health`) и локальный (`/health` для Docker HEALTHCHECK, `/status`). Синхронные обработчики — FastAPI уводит их в threadpool; задания всё равно идут по одному (блокировка в `JobProcessor`). |
| **stdlib `hmac`/`hashlib`/`ssl`/`threading`/`zoneinfo`** | `auth.py` (проверка подписи), `ad.py` (проверка сертификата LDAPS), `main.py` (потоки, таймзона дневной сводки), `jobs.py`/`reconcile.py` (блокировки) | Не тянуть лишние зависимости туда, где хватает стандартной библиотеки. |
| **pytest / ruff / mypy --strict** | dev-инструменты | Обязательный чистый прогон перед завершением любого этапа: `ruff format` + `ruff check` + `mypy` + `pytest`. |

Зачем **не** взяли: `asyncio` — сервис синхронный по архитектуре (единицы запросов от одного сайта, LDAP-операции блокирующие); APScheduler — простой идиом `while not stop.wait(interval)` на `threading.Event` даёт то же самое без лишней зависимости; ORM — журнал в SQLite достаточно простой, чтобы писать SQL руками; собственный слой бизнес-уведомлений (`Notifier`/боты) — логи уже уходят в Loki, этого достаточно (см. раздел 4).

## 3. Применённые паттерны

| Паттерн | Где | Почему |
|---|---|---|
| **Protocol (структурная типизация)** | `DirectoryGateway` (`ad.py`), `JobHandler` (`handlers.py`) | Узкие интерфейсы — боевая реализация (`AdGateway`) и тестовый фейк (`tests/fakes.py`) взаимозаменяемы без наследования; `jobs.py`/`reconcile.py` зависят только от Protocol. |
| **Strategy** | Реестр `handlers: dict[str, JobHandler]`, собираемый в `main.py` | Новый тип события от WP = новый класс-обработчик + строка в словаре; ни `jobs.py`, ни существующие обработчики не трогаются (SOLID-O). |
| **Adapter/Gateway** | `AdGateway` (ldap3 → LDAPS) | Оборачивает протокол в узкий интерфейс, которым пользуются `handlers.py`/`reconcile.py` — сами не знают про `ldap3`. Недоступность DC превращает в `DirectoryUnavailableError` (API → `503`). |
| **Repository** | `repository.py` (`JobRepository`) | Единственная точка доступа к SQLite; append-only запись (`record`), никаких `UPDATE` откуда-либо ещё; агрегаты (`dead_count`, `daily_counts`, `status_counts`, `recent_entries`) — тонкие read-методы, добавлялись по мере реальной надобности конкретному потребителю, не заранее. |
| **Composition root** | `main.py` | Единственное место, где создаётся `Settings()`, разворачиваются секреты, собираются все зависимости и стартуют потоки/`uvicorn`. Больше нигде в коде нет глобального состояния или синглтонов. |

## 4. Как расширять сервис

### Добавление канала оповещений (боты и т.п.)

Логи уже уходят в Grafana Loki (`LOKI_URL`) — общий контейнер для всей инфраструктуры (тот же, что у `fs-video-uploader`). **Это основной и единственный канал наблюдаемости** — сервис не встраивает Telegram/другие боты напрямую и не шлёт им push. Уровни логов расставлены осознанно под это:

- INFO — успешные операции (создание/реактивация учётки, `deprovision` → `done`, сверка без abort);
- WARNING — некритичные аномалии (незнакомый `subject_key`);
- ERROR — требует внимания (вне управляемой зоны, dead-задание, abort сверки, сбои сети к LMS/AD) — по `level="error"` в Loki настраивается Grafana Alerting с отправкой в мессенджер, если/когда это понадобится. Настройка — в Grafana (LogQL-правило + contact point), не в этом репозитории.

Чтобы добавить ещё один канал логов (например, Elastic): новый `logging.Handler` в `logging_setup.py` (`emit()`, ошибки — через `self.handleError(record)`, не собственный `try/except`), новая опциональная переменная в `Settings`, ветка в `configure_logging()`. Остальной код трогать не нужно — все модули логируют через `logging.getLogger("adsync.<module>")`, и всё, что подключено в `configure_logging`, получает запись автоматически.

### Добавление нового направления (subject)

Кода не требует. `config/subjects.yaml` — карта `subject_key → {ou_dn, group_dn}`, читается и валидируется один раз при старте:

```yaml
subjects:
  new-subject:
    ou_dn: "OU=Новое направление,OU=Ученики,...,DC=fs,DC=loc"
    group_dn: "CN=NewGroup,OU=Группы,...,DC=fs,DC=loc"
```

`subject_key`, которого нет в карте, не роняет обработку — `ProvisionHandler` создаёt учётку в `AD_OU_FALLBACK` без группы, пишет WARNING в лог и продолжает (см. `.docs/AdSync_API.md`, раздел про `provision`). Разные `subject_key` могут вести в одну и ту же OU/группу — это штатно.

### Добавление нового типа события от WP

`handlers.py`: новый класс `JobHandler` (реализует `handle(self, job: Job) -> HandlerResult`), новая модель в дискриминированном union `models.py`, новая строка в реестре `main.py`. `jobs.py` не меняется — он диспетчеризует по `job.event` вслепую.

### Замена AD на другую директорию/протокол

`handlers.py`/`jobs.py`/`reconcile.py` зависят не от `ldap3`, а от узкого `DirectoryGateway` (`ad.py`):

```python
class DirectoryGateway(Protocol):
    def find_user(self, username: str) -> DirectoryUser | None: ...
    def create_user(self, *, ou_dn: str, username: str, first: str, last: str) -> str: ...
    def ensure_password(self, dn: str, password: str) -> None: ...
    def ensure_enabled(self, dn: str) -> None: ...
    def ensure_disabled(self, dn: str) -> None: ...
    def ensure_group_membership(self, user_dn: str, group_dn: str) -> None: ...
    def move_to_ou(self, dn: str, target_ou_dn: str) -> str: ...
    def is_in_managed_zone(self, dn: str) -> bool: ...
    def is_in_disabled_ou(self, dn: str) -> bool: ...
    def list_zone_accounts(self) -> list[ZoneAccount]: ...
    def verify_zone_exists(self) -> None: ...
    def ping(self) -> None: ...
```

Новая реализация этого протокола в новом модуле + замена `AdGateway(...)` на неё в `main.py` (в трёх местах: задания, сверка, проверка связи — у каждого своё LDAP-соединение) — весь остальной код не заметит разницы.

## 5. Как пользоваться

### Настройки

Полный список переменных — `.env.example`. Коротко по группам:

- **Подпись**: `FS_LMS_AD_HMAC_SECRET` (совпадает с `wp-config.php` плагина), `HMAC_MAX_SKEW_SECONDS`.
- **Публичный API**: `PUBLIC_PORT` (8443), `TLS_CERT_FILE`, `TLS_KEY_FILE`.
- **Сверка**: `RECONCILE_GRACE_MINUTES`, `RECONCILE_MAX_DISABLE`, `RECONCILE_MAX_DISABLE_PCT` — предохранители против массового отключения по сбойным данным; когда и в каком режиме сверять, решает сайт.
- **AD**: `LDAP_HOST`, `LDAP_PORT` (только LDAPS), `LDAP_CA_CERT`, `LDAP_BIND_DN`/`LDAP_BIND_PASSWORD` (сервис-аккаунт), `AD_UPN_SUFFIX`, `AD_OU_DISABLED`, `AD_OU_FALLBACK`, `SUBJECTS_FILE`.
- **Рантайм**: `DATA_DIR` (`state.db` + логи), `TZ_NAME`, `DAILY_SUMMARY_TIME` (пусто = сводка выключена).
- **Опция**: `LOKI_URL`.
- **Локальный API**: `API_PORT` (по умолчанию 8091 — 8090 занят `fs-video-uploader`, не путать).

Обязательные поля (без них процесс не стартует — падает сразу, а не на первом задании): `FS_LMS_AD_HMAC_SECRET` (непустой), `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD`, `AD_OU_DISABLED`, `AD_OU_FALLBACK`, файлы `TLS_CERT_FILE`/`TLS_KEY_FILE`.

### Запуск локально

Нужен [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env                              # секрет подписи, AD-реквизиты, пути TLS
cp config/subjects.yaml.example config/subjects.yaml   # прописать свои направления
uv run python src/main.py
```

Сервис поднимет публичный API на `PUBLIC_PORT` (TLS), локальный — на `API_PORT`, и поток heartbeat (плюс дневная сводка, если задан `DAILY_SUMMARY_TIME`).

### Деплой на реальный сервер (LXC в Proxmox)

Сервис рассчитан на отдельный **непривилегированный LXC-контейнер** (Ubuntu) в технической сети учебного центра, с Docker + Compose внутри. Ниже — полный чек-лист от чистого LXC до работающего сервиса.

#### Требования к LXC

- Сеть: входящий `PUBLIC_PORT` от сайта через проброс на роутере (разрешён только исходящий IP сайта) и исходящий LDAPS (порт 636) до домен-контроллера.
- Docker + Docker Compose plugin.
- **NTP обязателен** (`timedatectl set-ntp true` или `chrony`/`systemd-timesyncd`) — подпись запросов сайта проверяется в окне ±`HMAC_MAX_SKEW_SECONDS` (300); рассинхрон часов LXC даёт `401` на каждый запрос сайта.
- Сертификат DC для проверки LDAPS (`LDAP_CA_CERT`) — получить у администратора домена; **отключать верификацию сертификата запрещено**.

#### Что нужно подготовить заранее

| Что | Где взять | Куда идёт |
|---|---|---|
| `FS_LMS_AD_HMAC_SECRET` | генерируется кнопкой в админке fs-lms («Синхронизация с доменом (AD)») | `.env` + `wp-config.php` сайта |
| TLS-сертификат публичного API | `openssl req -x509 …` с белым IP офиса в SAN (команда — README, «Подключение к сайту») | `./config/tls/server.{crt,key}`; копия `server.crt` — на хостинг сайта (`FS_LMS_AD_SERVER_CERT`) |
| `LDAP_BIND_DN`/`LDAP_BIND_PASSWORD` | сервис-аккаунт `svc-adsync` с делегированием только на управляемую зону (создание/изменение/`modify_dn` учёток-пользователей, reset password, запись `member` групп направлений) | `.env` |
| `LDAP_CA_CERT` | сертификат/цепочка DC (роль AD CS или self-signed) | `./config/ldap-ca.pem` на хосте |
| `AD_OU_DISABLED`, `AD_OU_FALLBACK` | DN OU «Отчисленные» и «Без направления» — создаются заранее администратором домена | `.env` |
| DN OU/групп по каждому направлению | админ домена | `config/subjects.yaml` |
| (опц.) `LOKI_URL` | адрес Grafana Loki push API, если используется централизованное логирование | `.env` |

Без `FS_LMS_AD_HMAC_SECRET`/`LDAP_BIND_DN`/`LDAP_BIND_PASSWORD`/`AD_OU_DISABLED`/`AD_OU_FALLBACK` и файлов TLS сервис не стартует.

#### Карта направлений (`subjects.yaml`)

`config/subjects.yaml` читается **один раз при старте** (валидация через pydantic; ошибка схемы = падение с понятным сообщением). Хот-релоада нет:

```bash
cp config/subjects.yaml.example config/subjects.yaml   # первый раз
# отредактировать config/subjects.yaml
docker compose restart adsync   # применить изменения
```

Дополнительно на старте `AdGateway` проверяет существование в AD каждого `ou_dn`/`group_dn` из карты, а также `AD_OU_DISABLED`/`AD_OU_FALLBACK` — опечатка в DN не всплывёт на первом задании, а уронит процесс сразу со списком всех отсутствующих DN. Файл монтируется в контейнер только на чтение.

#### Запуск

```bash
cp .env.example .env                                    # заполнить секреты из чек-листа выше
cp config/subjects.yaml.example config/subjects.yaml     # заполнить реальные направления
docker compose up -d --build
```

Что делает `docker-compose.yml`:
- `./data:/data` — здесь живут `state.db` и файловые логи, переживают пересоздание контейнера.
- `./config:/app/config:ro` — `subjects.yaml`, `ldap-ca.pem` и `tls/` монтируются только на чтение; `server.key` должен принадлежать uid 1000.
- `ports` — `PUBLIC_PORT` (8443, для проброса на роутере) и `API_PORT` (8091, только внутри сегмента).
- `env_file: .env` — все переменные окружения сервиса.
- `healthcheck` — `GET /health` каждые 30с (через `python -c` с `urllib.request`, без установки `curl` в образ), для `docker compose ps`/оркестраторов.
- `restart: unless-stopped` — переживает падения и перезагрузку хоста.

`Dockerfile` — двухстадийная сборка на `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`: `uv sync --frozen --no-dev` в builder-стадии, затем весь `/app` (готовый `.venv` + `src/`) одним `COPY --from=builder --chown=...` в финальный слой от непривилегированного пользователя (`uid 1000`) — без `uv`/`uv.lock`/тестов в рантайм-образе. Тот же приём, что в `Dockerfile` `fs-video-uploader`.

#### Проверка после деплоя

```bash
curl -s http://localhost:8091/health     # {"status": "ok", ...}
curl -s http://localhost:8091/status     # счётчики журнала, изначально нулевые
```

С сайта — «Проверить соединение» в настройках модуля (подписанный `GET /v1/health`). Справочник API с примерами — `.docs/AdSync_API.md`, нормативный контракт — `.docs/AdSyncPythonService.md`.

#### Метрики и алерты в Grafana (лейбл `event`)

Записи лога на значимых бизнес-событиях и основных инфраструктурных шагах несут третий
Loki-лейбл `event` (рядом с `service`/`level`) — фиксированное машинное имя, не зависящее
от формулировки русского текста. Передаётся через `extra={"event": "..."}` на вызывающей
стороне, читается `LokiHandler.emit()` (`src/logging_setup.py`). Синхронизировано с тем же
решением в `fs-video-uploader` (см. его `.docs/basic_doc.md`, раздел «Метрики и алерты в
Grafana») — оба сервиса пишут в общий Loki. Логи без привязки к конкретному шагу (granular
LDAP-идемпотентность в `ad.py`, отключённый access-лог uvicorn и т.п. — см.
`.docs/Events-Logging.md`) лейбла `event` не несут.

| `event` | Уровень | Что означает | Файл |
|---|---|---|---|
| `job_received` | INFO | Началась обработка задания, присланного сайтом (`POST /v1/jobs`) | `jobs.py` |
| `job_done` | INFO | Задание выполнено, сайту ушёл `done` | `jobs.py` |
| `job_failed` | INFO | Задание не выполнено, сайту ушёл `failed` (уровень записи всё ещё INFO — сам факт неуспеха отражён в `event`, не в уровне) | `jobs.py` |
| `job_handler_missing` | ERROR | В задании `event`, для которого нет зарегистрированного обработчика | `jobs.py` |
| `job_handler_error` | ERROR (exception) | Необработанное исключение внутри `JobHandler.handle()` | `jobs.py` |
| `job_dead` | ERROR | ≥6 неудач подряд по одному `idempotency_key` — нужен администратор | `jobs.py` |
| `job_invalid` | ERROR | Тело `POST /v1/jobs` не прошло валидацию — сайту ушёл `422` | `api.py` |
| `reconcile_invalid` | ERROR | Тело `POST /v1/reconcile` не прошло валидацию — `422` | `api.py` |
| `signature_rejected` | WARNING | Запрос с неверной или просроченной подписью — `401` (повторяется — проверить секрет и часы) | `api.py` |
| `api_started` | INFO | Подняты публичный (TLS) и локальный API | `main.py` |
| `account_created` | INFO | Provision: учётки не было, создана в OU направления (или fallback) | `handlers.py` |
| `account_reactivated` | INFO | Provision: учётка была в OU «Отчисленные», реактивирована и перенесена обратно | `handlers.py` |
| `account_updated` | INFO | Provision: учётка уже в управляемой зоне, приведена к целевому состоянию | `handlers.py` |
| `account_deprovisioned` | INFO | Deprovision выполнен — отключена и перенесена (или уже был идемпотентный случай: учётки нет / уже отключена) | `handlers.py` |
| `password_changed` | INFO | Пароль учётки заменён на заданный администратором на сайте | `handlers.py` |
| `password_account_missing` | ERROR | Смена пароля: учётки нет в домене — сайту ушёл `failed` | `handlers.py` |
| `subject_unmapped` | WARNING | `subject_key` не найден в `subjects.yaml`, учётка создаётся в fallback-OU | `handlers.py` |
| `zone_violation` | ERROR | Учётная запись вне управляемой зоны — ни `provision`, ни `deprovision` её не трогают | `handlers.py` |
| `reconcile_done` | INFO | Прогон сверки завершён, лишние учётки отключены | `reconcile.py` |
| `reconcile_dry_run` | INFO | Сверка в режиме «только журнал»: перечислены учётки, которые были бы отключены | `reconcile.py` |
| `reconcile_aborted` | ERROR | Сверка отменена любым предохранителем (пустой список, превышен порог) — никто не тронут | `reconcile.py` |
| `ad_reconnect` | WARNING | Соединение с AD потеряно, выполняется переподключение | `ad.py` |
| `ad_unavailable` | ERROR | DC недоступен и после переподключения — сайту уходит `503` | `ad.py` |
| `cn_collision` | WARNING | В целевой OU уже есть объект с таким CN, но с другим `sAMAccountName` (тёзка) — подбирается следующий вариант CN с логином в скобках | `ad.py` |
| `service_started` | INFO | Сервис запущен (лог сразу после `configure_logging`) | `main.py` |
| `service_stopped` | INFO | Сервис полностью остановился (конец `main()`, после закрытия клиентов) | `main.py` |
| `shutdown_signal_received` | INFO | Получен SIGTERM/SIGINT, начат graceful shutdown | `main.py` |
| `heartbeat` | INFO | Раз в `HEARTBEAT_INTERVAL_SECONDS` — сервис жив, сводка журнала (`done`/`failed`/`dead`) | `main.py` |
| `daily_summary` | INFO | Суточная сводка (создано/отключено/ошибок за 24ч), если включена `DAILY_SUMMARY_TIME` | `main.py` |
| `background_loop_error` | ERROR (exception) | Необработанная ошибка тика фонового цикла (`heartbeat`) | `main.py` |
| `daily_summary_loop_error` | ERROR (exception) | Необработанная ошибка в потоке дневной сводки | `main.py` |

Примеры LogQL-запросов (Grafana → Explore/Dashboard, источник данных Loki):

```logql
# счётчик созданных учёток за час
sum(count_over_time({service="fs-adsync", event="account_created"}[1h]))

# все события, сгруппированные по типу, за сутки
sum by (event) (count_over_time({service="fs-adsync"}[24h]))

# только dead-задания
{service="fs-adsync", event="job_dead"}
```

Кандидаты на алерты (см. также `.docs/Events-Logging.md`):

- `event="job_dead"` (ERROR) — задание не проходит уже 6 попыток подряд, нужен администратор.
- `event="zone_violation"` (ERROR) — попытка тронуть учётку вне управляемой зоны (provision или deprovision).
- `event="reconcile_aborted"` (ERROR) — сверка не выполнена ни разу за прогон, зона могла разъехаться с сайтом.
- `event="ad_unavailable"` (ERROR) — DC недоступен; задания копятся в очереди на сайте.
- `event="signature_rejected"` (WARNING), повторяющийся — рассинхрон секрета или часов с сайтом.
- `event="subject_unmapped"` (WARNING) — нужно дополнить `subjects.yaml`.
- `event="heartbeat"` — **отсутствие** дольше `2 × HEARTBEAT_INTERVAL_SECONDS` (`absent_over_time`) сигнализирует, что процесс умер молча — тот же паттерн, что в `fs-video-uploader`.
