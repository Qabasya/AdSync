# fs-AdSync

![CI](https://github.com/Qabasya/AdSync/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white)
![uv](https://img.shields.io/badge/package%20manager-uv-de5fe9)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-v2-e92063?logo=pydantic&logoColor=white)
![LDAP](https://img.shields.io/badge/LDAPS-Active%20Directory-0078D4)
![mypy](https://img.shields.io/badge/mypy-strict-blue)
![ruff](https://img.shields.io/badge/lint-ruff-black)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)

Фоновый сервис-поллер, который создаёт и сопровождает доменные учётные записи учеников в Active Directory по заявкам из LMS (WordPress-плагин [fs-lms](https://github.com/Qabasya/fs-lms), модуль `AdSync`).

## Что он делает

1. Раз в `JOBS_POLL_SECONDS` секунд забирает у WP готовые задания (`GET /ad/jobs`) — `provision` (создать/реактивировать учётку) или `deprovision` (отключить); промежуточной стадии «зачислен» в OU-структуре нет.
2. Выполняет задание в Active Directory по LDAPS: новую учётку — сразу в OU направления по `subjects.yaml`, с паролем и членством в security-группе; при повторной выдаче — идемпотентно (уже существует/уже в группе — не ошибка).
3. Незнакомый `subject_key` не роняет обработку — учётка создаётся в OU «Без направления», в лог уходит WARNING, маппинг администратор дополняет позже.
4. Отчитывается по каждому заданию (`POST /ad/ack`, `done`/`failed`) — обязательно, и при успехе, и при ошибке; после 6 неудач подряд по одному заданию оно считается «мёртвым», сигнал — ERROR в лог.
5. Раз в `RECONCILE_INTERVAL_HOURS` часов сверяет список активных логинов от WP (`GET /ad/active-usernames`) с реальными учётками управляемой зоны в AD и отключает лишних — с предохранителями (порог по количеству/проценту, grace-период для свежих учёток, отмена при пустом списке), чтобы сбойные данные от WP не отключили половину домена разом.
6. Hard-delete учёток запрещён всегда — только отключение и перенос в OU «Отчисленные».

Всё состояние (что обработано, с каким статусом, сколько раз подряд падало) — в SQLite, append-only журнал, доступно через HTTP `/status`.

## Установка и запуск локально

Нужен [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env                                    # заполнить LMS/AD реквизиты
cp config/subjects.yaml.example config/subjects.yaml     # прописать свои направления
uv run python src/main.py
```

Сервис поднимет HTTP API на `API_PORT` (по умолчанию 8091) и будет опрашивать LMS/сверять AD в фоне.

## Настройка

Всё через переменные окружения / `.env` (см. `.env.example` — там расписана каждая переменная). Ключевое:

- **`LMS_BASE_URL` / `FS_LMS_AD_HMAC_SECRET`** — куда стучаться и чем подписывать запросы к fs-lms (секрет должен совпадать с тем, что в wp-config плагина).
- **`LDAP_HOST` / `LDAP_BIND_DN` / `LDAP_BIND_PASSWORD` / `LDAP_CA_CERT`** — доступ к домен-контроллеру по LDAPS (сертификат обязателен, отключать проверку нельзя).
- **`SUBJECTS_FILE`** — `subjects.yaml`, маппинг «направление → OU + группа в AD» (пример в `config/subjects.yaml.example`). `subject_key`, которого нет в файле, не роняет обработку — уйдёт в fallback-OU.
- **`AD_OU_DISABLED` / `AD_OU_FALLBACK`** — DN OU «Отчисленные» и «Без направления».
- **`RECONCILE_MAX_DISABLE` / `RECONCILE_MAX_DISABLE_PCT` / `RECONCILE_GRACE_MINUTES`** — предохранители цикла сверки.
- **`LOKI_URL`** — если задан, логи параллельно улетают в Grafana Loki. Это основной канал наблюдаемости — телеграм-уведомлений сервис сам не шлёт, для этого будет Grafana Alerting поверх Loki (как и у `fs-video-uploader`).

Обязательные поля (без них сервис не стартует — упадёт сразу с понятной ошибкой, а не по пути): `LMS_BASE_URL`, `FS_LMS_AD_HMAC_SECRET`, `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD`, `AD_OU_DISABLED`, `AD_OU_FALLBACK`.

## HTTP API

- `GET /health` — жив ли процесс, время последнего тика заданий/сверки.
- `GET /status` — счётчики (`done`/`failed`/`dead`) + последние 20 записей журнала.
- `POST /reconcile` — внеочередной запуск сверки, не дожидаясь `RECONCILE_INTERVAL_HOURS`; ответ синхронный (результат прогона в теле).

Подробнее с примерами запросов/ответов, а также контракт исходящих запросов к fs-lms (HMAC-подпись, формат заданий, обработка ошибок) — в `.docs/AdSync_API.md`.

## Деплой (Docker)

```bash
cp .env.example .env
cp config/subjects.yaml.example config/subjects.yaml
docker compose up -d --build
```

`docker-compose.yml` монтирует:

- `./data` — SQLite-журнал и файловые логи, переживают рестарт контейнера,
- `./config` — `subjects.yaml` (и `ldap-ca.pem`, если используется), read-only.

Healthcheck (задан в `docker-compose.yml`) дёргает `/health` изнутри контейнера. Логи, помимо файла, можно направить в Loki через `LOKI_URL`.

## Разработка

```bash
uv run ruff format . && uv run ruff check .   # форматирование и линт
uv run mypy src                               # типы, strict-режим
uv run pytest                                 # тесты
```

Всё то же самое гоняется в CI на каждый PR/push в `main`.
