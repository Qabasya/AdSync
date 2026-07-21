# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`fs-adsync` — фоновый сервис-поллер, который создаёт и сопровождает доменные учётки учеников по заявкам из LMS (WordPress-плагин `fs-lms`, модуль `Inc\Modules\AdSync`).

Модель взаимодействия:

- WP — публичный сайт; домен-контроллер — в локальной сети без белого IP. Push от WP невозможен, поэтому модель строго **pull**: сервис сам исходящими HTTPS-запросами забирает задания у WP, выполняет их в Active Directory по LDAPS и отчитывается ack'ом. Входящих запросов от WP не существует.
- От заявки на сайте до входа ученика за компьютер должны проходить **секунды** — цикл заданий крутится каждые ~3 с.
- WP-сторона готова и не меняется. REST-контракт и формула HMAC нормативно описаны в `docs/FS_LMS_API.md` (раздел AdSync); бизнес-ТЗ — `docs/AdSyncPythonService.md`. **Оба документа обязаны лежать в репозитории и читаются до написания любого кода.** Этот CLAUDE.md их не дублирует, а фиксирует принятые решения поверх.
- Жизненный цикл учётки упрощён относительно ТЗ (согласовано с администратором домена): OU `Pending`/`Active` **не используются** — `provision` кладёт ученика сразу в OU направления, где уже действуют существующие GPO. Отчисление = отключение учётки + перенос в OU «Отчисленные». Hard-delete запрещён всегда.

Два цикла:

```
jobs      (каждые JOBS_POLL_SECONDS):      GET /ad/jobs → обработчик → POST /ad/ack → журнал
reconcile (каждые RECONCILE_INTERVAL_HOURS): GET /ad/active-usernames → отключить лишних (с предохранителями)
```

## Commands

```bash
uv sync                          # окружение + зависимости (включая dev)
uv run adsync                    # локальный запуск сервиса
uv run pytest                    # тесты
uv run ruff format .             # автоформат (PEP 8)
uv run ruff check --fix .        # линт
uv run mypy src                  # проверка типов (strict)
docker compose up -d --build      # прод-запуск
```

Обязательная проверка перед завершением любого этапа (должна проходить чисто):

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest
```

- **pip / poetry / pdm не использовать.** Только uv: зависимости добавлять через `uv add <pkg>` (dev: `uv add --dev <pkg>`); `uv.lock` коммитится.
- Новые зависимости — только после явного согласования с пользователем.

## Tech Stack

- Python 3.12+, менеджер — **uv** (`pyproject.toml` + `uv.lock`, src-layout)
- pydantic v2 + pydantic-settings — `Settings`, валидация `subjects.yaml`, discriminated union заданий
- httpx — WP REST, push в Loki
- ldap3 — LDAPS; установка пароля только через `extend.microsoft.modify_password`
- PyYAML — `subjects.yaml` (dev: `types-PyYAML` для mypy strict)
- sqlite3 (stdlib) — журнал; **без ORM и без SQLAlchemy**
- FastAPI + uvicorn — тонкий локальный API (`/health`, `/status`, `/reconcile`)
- stdlib вместо зависимостей: `hmac` + `hashlib` (подпись запросов), `ssl` (валидация LDAPS по CA), `threading` (два daemon-цикла), `zoneinfo` (время)
- dev: pytest, ruff, mypy

Запрещено добавлять без явного запроса: APScheduler, EventBus-слой, DI-контейнеры, asyncio, requests, python-telegram-bot, cryptography, любой ORM.

## Code Style

- PEP 8 через `ruff format` + `ruff check`; line-length 100; конфиг в `pyproject.toml`
- `mypy --strict`; типы у всех параметров и возвращаемых значений
- ООП: логика в классах, зависимости — через конструктор; интерфейсы — `typing.Protocol`
- Внешние DTO и конфиги — pydantic; внутренние значения — frozen `dataclass(slots=True)` по необходимости
- Там, где логика зависит от времени (reconcile, grace-период), время передаётся зависимостью: `now: Callable[[], datetime]` в конструкторе — тестируется подстановкой, freezegun не нужен
- Пути — только `pathlib.Path`
- `print` запрещён — только `logging` (логгеры `adsync.<module>`)
- Docstrings (Google style) на публичных классах и методах
- Никакого глобального изменяемого состояния и синглтонов; вся сборка зависимостей — в composition root (`main.py`)

## SOLID в этом проекте

SOLID здесь — про границы, а не про количество паттернов:

- **S** — модуль отвечает за одно: poller не знает про LDAP, шлюзы не знают про бизнес-правила, репозиторий не знает про ack.
- **O** — новый тип события от WP = новый класс-обработчик + строка в реестре-словаре в `main.py`; существующий код не правится.
- **L** — реализации Protocol полностью взаимозаменяемы (боевые классы и фейки в тестах).
- **I** — три узких Protocol: `JobHandler`, `LmsApi`, `DirectoryGateway`.
- **D** — httpx / ldap3 / sqlite3 живут только внутри `lms.py` / `ad.py` / `repository.py` (httpx дополнительно — в лог-хендлерах `logging_setup.py`); остальной код зависит от Protocol; конкретика подключается в `main.py`.

Сознательно **не** вводим: EventBus и отдельный слой бизнес-уведомлений (вместо них — единый канал `logging`, а мессенджер-алертинг, если понадобится, настраивается поверх Loki в Grafana Alerting, не в коде сервиса), APScheduler (два daemon-потока `while not stop.wait(interval)`), вложенные пакеты (модули плоские — семейств стратегий здесь нет), use-case-слои, иерархии собственных исключений.

## Architecture

| Модуль (`src/adsync/`) | Роль |
|---|---|
| `main.py` | Composition root: `Settings` → сборка зависимостей → реестр обработчиков (обычный dict `{"provision": …}`) → два daemon-потока + uvicorn; graceful shutdown по SIGTERM |
| `config.py` | `Settings` (pydantic-settings) + загрузка и валидация `subjects.yaml` |
| `models.py` | `ProvisionJob \| DeprovisionJob` — discriminated union по полю `event`; DTO ack |
| `lms.py` | `LmsApi` (Protocol) + `LmsClient` (httpx): `get_jobs` / `ack` / `get_active_usernames`; HMAC-подпись; таймауты; **без собственных ретраев** |
| `ad.py` | `DirectoryGateway` (Protocol) + `AdGateway` (ldap3): идемпотентные операции, контроль управляемой зоны, переподключение при обрыве |
| `handlers.py` | `JobHandler` (Protocol) + `ProvisionHandler` / `DeprovisionHandler` |
| `poller.py` | Цикл заданий: fetch → dispatch → ack → журнал; последовательная обработка |
| `reconcile.py` | Сверка: список от WP против активных учёток зоны; предохранители |
| `repository.py` | SQLite-журнал (append-only), dead-счётчик; ни пароля, ни сырых payload |
| `logging_setup.py` | Хендлеры file / loki + redaction-фильтр пароля — единственный канал уведомлений; мессенджер-алертинг (если понадобится) — поверх Loki в Grafana |
| `api.py` | FastAPI: `GET /health`, `GET /status`, `POST /reconcile` — без бизнес-логики |

### Design Patterns

- **Strategy** — обработчики заданий за `JobHandler`; реестр — обычный dict в `main.py`, без фабрик-фреймворков и регистраций через декораторы.
- **Adapter/Gateway** — `LmsClient` (httpx), `AdGateway` (ldap3).
- **Repository** — `repository.py`, единственная точка доступа к SQLite.

Наблюдать за WP событийно невозможно — push исключён сетевой топологией, обнаружение заданий строго опросом. Не предлагать вебхуки, входящие эндпоинты и очереди сообщений.

## Contracts

### REST (WP; нормативный источник — `docs/FS_LMS_API.md`)

- База: `{LMS_BASE_URL}` (`https://…/wp-json/fs-lms/v1`).
- Подпись **каждого** запроса: заголовки `X-Fs-Timestamp` + `X-Fs-Signature`, секрет `FS_LMS_AD_HMAC_SECRET` (тот же, что в `wp-config.php`; генерируется в админке WP). Точная формула — `FS_LMS_API.md` §2; реализация строго по ней, на stdlib `hmac`+`hashlib`.
- `GET /ad/jobs?limit={JOBS_LIMIT}` → массив заданий (состав payload зависит от `event`). Задание может **исчезнуть** из выдачи (заявку удалили) — это не ошибка, ack не требуется.
- `POST /ad/ack` `{id, status, error?}` — **обязателен на каждое обработанное задание**, и при успехе, и при ошибке. Слать строго `done` / `failed`. `sam_account_name` не отправляем — WP его не использует.
- `GET /ad/active-usernames` → `{"usernames": […]}` — авторитетный список логинов, обязанных оставаться активными.
- **Ретраи и backoff — целиком на стороне WP**: наш `ack(failed)` ⇒ WP переотдаст задание с экспоненциальной задержкой (1м → … → 1ч), после 6 неудач задание «мёртвое» и из выдачи исчезает. Сетевой сбой самого опроса → просто следующий тик; локальных ретраев у клиента нет.

### События → действия в AD

| `event` | payload | Действие |
|---|---|---|
| `provision` | `id, event, idempotency_key, username, password, first, last, subject_key` | Создать учётку в OU направления по `subjects.yaml`: `objectClass=user`, `sAMAccountName=username`, `userPrincipalName={username}@{AD_UPN_SUFFIX}`, `cn`/`displayName` из `first`+`last`; создаётся **отключённой** (`userAccountControl=514`) — AD отклоняет `add()` сразу включённой учётки без пароля (`WILL_NOT_PERFORM`/5003); пароль `unicodePwd` только по LDAPS через `extend.microsoft.modify_password`, и только **после** этого — включение (`userAccountControl=512`); `MODIFY_ADD` в security-группу направления |
| `deprovision` | `id, event, idempotency_key, username` | `userAccountControl=514` + `modify_dn` в `AD_OU_DISABLED` |

Событий ровно два — никакой промежуточной стадии «зачислен» в OU-структуре нет: учётка создаётся
один раз сразу в целевой OU направления и остаётся там до отчисления. Событие `promote` в контракте
fs-lms не существует (см. `.docs/FS_LMS_API.md`, §3.1, и `.docs/AdSyncPythonService.md`, §3.1) — в
этом сервисе такой обработчик когда-то был реализован как задел, но убран при сверке с нормативными
документами модуля `Inc\Modules\AdSync`.

Правила поверх таблицы:

- **Незнакомый `subject_key`**: создать учётку в `AD_OU_FALLBACK` без группы направления → `done` + WARNING в лог. Не падать — ученик должен войти немедленно, маппинг админ дополнит потом.
- **provision, а учётка уже существует**: в OU управляемой зоны → ensure (пароль из payload + включена + членство в группе — включение обязательно и здесь, не только при создании: если предыдущая попытка упала между паролем и включением, повторная выдача не должна оставить учётку висеть отключённой навсегда) → `done`; в `AD_OU_DISABLED` → **реактивация** (включить, перенести в OU направления по `subject_key`, пароль, группа) → `done`; **вне управляемой зоны → `failed` + ERROR, объект не трогать** — это чужая учётка.
- **deprovision**: учётки нет → `done` (цель достигнута); уже отключена → `done`; вне зоны → `failed` + ERROR.
- Обработчики **идемпотентны**: повторная выдача задания (потерянный ack, падение посреди обработки) переносится спокойно; «уже существует» / «уже в группе» / «уже отключена» — успех, не ошибка.
- `idempotency_key` — ключ журнала и dead-счётчика, **не барьер**: при повторной выдаче работа выполняется заново (идемпотентно) и ack отправляется снова.
- **Dead**: наш 6-й `ack(failed)` по одному `idempotency_key` ⇒ ERROR в лог. WP о «мёртвых» заданиях наружу не сообщает — этот сервис единственный источник тревоги.
- Успешные действия (создание учётки, реактивация, `deprovision` → `done`) логируются на уровне INFO — единый аудиторский след в логах, отдельного канала бизнес-событий нет.

### Сверка (reconcile)

- Управляемая зона: все `ou_dn` из `subjects.yaml` + `AD_OU_FALLBACK`. `AD_OU_DISABLED` — вне сверки.
- Активная учётка зоны, чьего `sAMAccountName` нет в списке от WP → путь deprovision (отключить + перенести в «Отчисленные»).
- **Предохранители** — нарушение любого ⇒ ничего не отключать, ERROR в лог:
  - пустой список при непустой зоне — abort всегда;
  - к отключению больше `RECONCILE_MAX_DISABLE` учёток — abort;
  - к отключению больше `RECONCILE_MAX_DISABLE_PCT`% зоны — abort.
- Grace: учётки с `whenCreated` моложе `RECONCILE_GRACE_MINUTES` не трогаются (гонка со свежим provision против чуть устаревшего списка).
- Сверка **односторонняя**: никого не включает, не создаёт и не переносит обратно.
- Успешный прогон без abort логируется на уровне INFO (сколько отключено, если есть).

### config/subjects.yaml

```yaml
subjects:
  inf-ege:                      # subject_key из WP (стабильный слаг направления)
    ou_dn: "OU=КЕГЭ,OU=Ученики,OU=Пользователи,OU=LocalOffice,DC=fs,DC=loc"
    group_dn: "CN=KEGE,OU=Группы,OU=LocalOffice,DC=fs,DC=loc"
```

- Ключ — `subject_key`; DN в примере иллюстративные, реальные подставляются при деплое.
- Разные ключи могут вести в одну OU/группу — это допустимо.
- Валидация pydantic на старте (непустые DN, корректная схема); ошибка → падение на старте с внятным сообщением.
- Дополнительно на старте `AdGateway` проверяет существование в AD каждого `ou_dn`/`group_dn` из карты, а также `AD_OU_DISABLED` и `AD_OU_FALLBACK`; любой отсутствующий DN → падение на старте с внятным сообщением (fail fast: опечатка в DN не должна всплывать на первом задании).

## Job Processing Rules

- Обработка последовательная, в порядке выдачи, один поток. `limit` до 100 после простоя сервиса — штатная ситуация.
- Ошибка одного задания (LDAP, сеть к DC, валидация) → `ack(failed, error=…)` и переход к следующему; цикл не падает.
- Ответы WP валидируются pydantic-моделями на входе; невалидное задание → `failed` + ERROR, без попытки обработки.
- **Пароль живёт только в памяти** от `get_jobs` до `ack`; не попадает в журнал, логи, уведомления и `/status` (redaction-фильтр — страховка, а не разрешение).
- SIGTERM: дообработать текущее задание, отправить его ack, новых заданий не брать, корректно закрыть соединения и выйти.

## State (SQLite)

- Файл `DATA_DIR/state.db`; режим WAL; доступ — только через `repository.py`; без ORM.
- Таблица `jobs` — append-only журнал обработок: `id`, `job_id`, `idempotency_key`, `event`, `username`, `subject_key` (NULL для `deprovision`), `status` (`done`|`failed`), `error` (NULL), `received_at`, `acked_at`. **Пароль и сырой payload не сохраняются никогда.**
- Dead-счётчик = `COUNT(status='failed')` по `idempotency_key`; отдельной машины состояний нет — журнал фактов.
- Времена в БД — UTC ISO 8601.

## Logging & Notifications

Единый канал: всё, что происходит в сервисе — успешные операции, предупреждения, ошибки —
проходит через стандартный `logging`. Отдельного слоя бизнес-уведомлений в коде сервиса нет.
Оба сервиса инфраструктуры (этот и fs-video-ingest) пишут в общий Loki; если понадобится
оповещение в мессенджер — это Grafana Alerting поверх Loki (LogQL-правило + contact point),
настраивается руками в Grafana, не в этом репозитории.

- Только stdlib `logging`; хендлеры собирает `logging_setup.py`:
  - file — `RotatingFileHandler` `DATA_DIR/logs/adsync.log` (10 MiB × 5), всегда включён;
  - loki — HTTP push (`/loki/api/v1/push`) при заданном `LOKI_URL`. **Loki общий для всей инфраструктуры** (тот же контейнер, что у fs-video-ingest); лейблы потока только низкокардинальные: `service="fs-adsync"`, `level`. `username`/`idempotency_key` — в тексте строки, не в лейблах.
- **Redaction-фильтр** (`logging.Filter`): значения полей `password`/`unicodePwd` вырезаются из любых записей до форматирования.
- Уровни осознанно расставлены по всему коду:
  - INFO — успешные операции: создание/реактивация учётки, `deprovision` → `done`,
    успешный прогон сверки (сколько отключено, если есть);
  - WARNING — некритичные аномалии, не требующие немедленной реакции: незнакомый `subject_key`;
  - ERROR — требует внимания: учётка/операция вне управляемой зоны, dead-задание (6-я подряд
    неудача по `idempotency_key`), abort сверки любым предохранителем, сбои сети к LMS/AD. Именно
    по `level="error"` в Loki будет строиться Grafana-алерт, если/когда его настроят.
- Дневная сводка (создано/отключено/ошибок за сутки; время `DAILY_SUMMARY_TIME`, пусто =
  выключено) — отдельный INFO-лог с агрегатами из `JobRepository.daily_counts(...)`, считается и
  пишется в `main.py`; уровень INFO — под Grafana-алерт по `level="error"` не подпадает, это
  плановый отчёт для file/Loki, не тревога.

## Configuration

`.env` → pydantic-settings. Секреты — только через env; `.env` в `.gitignore`; `.env.example` поддерживать актуальным.

| Переменная | Default | Назначение |
|---|---|---|
| `LMS_BASE_URL` | — | `https://…/wp-json/fs-lms/v1` |
| `FS_LMS_AD_HMAC_SECRET` | — | Секрет подписи (тот же, что в `wp-config.php`) |
| `JOBS_POLL_SECONDS` | `3` | Интервал цикла заданий |
| `JOBS_LIMIT` | `50` | `limit` в `GET /ad/jobs` (1–200) |
| `RECONCILE_INTERVAL_HOURS` | `6` | Интервал сверки |
| `RECONCILE_GRACE_MINUTES` | `15` | Grace-период для свежих учёток |
| `RECONCILE_MAX_DISABLE` | `10` | Порог: максимум отключений за проход |
| `RECONCILE_MAX_DISABLE_PCT` | `20` | Порог: максимум % зоны за проход |
| `LDAP_HOST` | `11.11.11.11` | Домен-контроллер |
| `LDAP_PORT` | `636` | Только LDAPS |
| `LDAP_CA_CERT` | `/app/config/ldap-ca.pem` | CA для валидации сертификата DC |
| `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD` | — | Сервис-аккаунт `svc-adsync` |
| `AD_UPN_SUFFIX` | `fs.loc` | Домен после `@` в UPN |
| `AD_OU_DISABLED` | — | DN OU «Отчисленные» |
| `AD_OU_FALLBACK` | — | DN OU «Без направления» (незнакомый `subject_key`) |
| `SUBJECTS_FILE` | `/app/config/subjects.yaml` | Карта направлений |
| `DATA_DIR` | `/data` | state.db + логи |
| `TZ_NAME` | `Europe/Moscow` | Время сводки и grace-расчётов |
| `DAILY_SUMMARY_TIME` | — | `HH:MM` дневной сводки (пусто = выкл) |
| `LOKI_URL` | — | Опция |
| `API_PORT` | `8091` | FastAPI (8090 занят fs-video-ingest — не путать) |

## HTTP API

- `GET /health` → `{"status": "ok", "last_jobs_poll_at": …, "last_reconcile_at": …}` — для Docker HEALTHCHECK.
- `GET /status` → счётчики (done/failed/dead) + последние 20 записей журнала (без чувствительных полей).
- `POST /reconcile` → внеочередной запуск сверки.
- API — строго локальный слой для админа и healthcheck; бизнес-логики не содержит. Входящих запросов от WP не существует по модели — не добавлять.

## Runtime Environment

- Отдельный LXC (Ubuntu) в Tech-сегменте `11.11.11.0/24`; внутри — Docker + Compose. CIFS не нужен.
- Сетевые потоки: исходящий HTTPS к сайту WP; исходящий LDAPS 636 к DC `11.11.11.11` (внутри сегмента — правила firewall не меняются).
- Тома: `./data:/data`, `./config:/app/config:ro` (`subjects.yaml`, `ldap-ca.pem`); `TZ=${TZ_NAME}`; `restart: unless-stopped`; HEALTHCHECK → `GET /health`.
- Dockerfile: multi-stage на `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, `uv sync --frozen --no-dev`, запуск от непривилегированного пользователя.
- LDAPS-соединение — с проверкой сертификата по `LDAP_CA_CERT` (stdlib `ssl`); **отключать верификацию запрещено**.

## Testing

- pytest; `tests/` зеркалит `src/`. Фейки: `FakeLmsApi`, `FakeDirectoryGateway`; журнал на tmp SQLite; время — подменой `now()`.
- В тестах запрещены: сеть, реальный AD/WP/Loki.
- Обязательное покрытие: HMAC-подпись (векторы из `FS_LMS_API.md`), валидация моделей заданий, оба обработчика (повторная выдача; существующая учётка в зоне / в «Отчисленных» (реактивация) / вне зоны; незнакомый `subject_key`), poller (ack при успехе и ошибке, невалидное задание, изоляция ошибок), reconcile (каждый предохранитель, grace, пустой список, односторонность), repository (dead-счётчик, отсутствие пароля в БД), redaction-фильтр (пароль не утекает в записи).
- Ручная проверка на живых системах — `scripts/smoke.py`: подписанный `GET /ad/jobs?limit=1` (без обработки) + LDAPS bind-check; вне pytest.

## CI (GitHub Actions)

- Workflow: `.github/workflows/ci.yml`. Триггеры: `pull_request` в `master` и `push` в `master`.
- Одна джоба на `ubuntu-latest`, Python 3.12: checkout → официальный `astral-sh/setup-uv` (актуальная мажорная версия, `enable-cache: true`) → `uv sync --frozen` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy src` → `uv run pytest`.
- Секреты в CI не нужны: тесты не ходят в сеть — это гарантировано разделом Testing.
- Мердж в `master` — только при зелёном CI. Branch protection (required status check `ci`) настраивается руками в GitHub.

## Strict Rules

- pip запрещён; зависимости — только через `uv add` и только по согласованию.
- **Пароль из payload никогда не логируется и не сохраняется** — ни в SQLite, ни в файлы, ни в уведомления, ни в `/status`; живёт в памяти от `get_jobs` до `ack`. Redaction-фильтр обязателен.
- httpx / ldap3 / sqlite3 — только внутри `lms.py` / `ad.py` / `repository.py` (httpx дополнительно — лог-хендлеры `logging_setup.py`); остальной код работает через Protocol.
- **Управляемая зона** = OU из `subjects.yaml` + `AD_OU_FALLBACK` + `AD_OU_DISABLED`. Объекты вне зоны сервис не читает и не изменяет; попытка операции над чужой учёткой → `failed` + ERROR.
- Hard-delete объектов AD запрещён всегда и везде.
- Сверка не может отключить больше порогов из конфига; пустой список при непустой зоне — всегда abort; сверка никого не включает.
- Ack (`done`/`failed` строго) обязателен на каждое обработанное задание.
- Обработчики идемпотентны; повторная выдача задания — штатная ситуация, не ошибка.
- Сервис не принимает входящих запросов от WP; FastAPI — только локальный health/status/reconcile.
- `print` запрещён; исключения не глотаются молча (минимум — лог с traceback).
- Не вводить EventBus, APScheduler, ORM, DI-контейнеры, asyncio, новые слои и зависимости без явного запроса.
- Работать поэтапно (план — в стартовом промпте): в конце этапа прогнать ruff + mypy + pytest и остановиться до подтверждения пользователя.

## Related Work (вне этого репозитория)

- **fs-lms**: модуль `Inc\Modules\AdSync` готов и под этот сервис не меняется. Секрет HMAC генерируется кнопкой в админке («Синхронизация с доменом (AD)») — единственный источник истины по текущему значению.
- **Домен (руками, до прода)**: LDAPS-сертификат на DC (роль AD CS или self-signed в хранилище DC; проверка `openssl s_client -connect 11.11.11.11:636`); OU «Отчисленные» и «Без направления»; сервис-аккаунт `svc-adsync` с делегированием только на управляемую зону (создание/изменение/`modify_dn` объектов-пользователей, reset password, запись `member` групп направлений). FGPP не требуется — парольная политика домена уже ослаблена (мин. длина 3, сложность отключена).
- **docs/**: до этапа 0 в репозиторий кладутся `AdSyncPythonService.md` и `FS_LMS_API.md` — источники истины по бизнес-логике и REST-контракту.
