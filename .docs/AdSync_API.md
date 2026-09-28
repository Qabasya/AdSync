# AdSync_API — HTTP API fs-adsync

Название файла — по аналогии с `VU_API.md` в репозитории `fs-video-uploader`. Сервис поднимает два
приложения FastAPI (`src/api.py`, сборка — `src/main.py`):

1. **Публичный API** — для сайта (fs-lms, модуль `AdSync`): задания, сверка, проверка связи.
   HTTPS на `PUBLIC_PORT` (8443), каждый запрос подписан. Нормативный контракт —
   `.docs/AdSyncPythonService.md`; этот документ — справочник с примерами.
2. **Локальный API** — для Docker HEALTHCHECK и админа: HTTP на `API_PORT` (8091), без
   аутентификации, наружу не пробрасывается.

Формат — везде `application/json`. Синхронные обработчики FastAPI уводит в пул потоков; задания при
этом обрабатываются строго по одному (одно LDAP-соединение).

---

## 1. Публичный API (сайт → сервис)

### Подпись

Каждый запрос несёт заголовки:

| Заголовок | Значение |
|---|---|
| `X-Fs-Timestamp` | unix-время, секунды |
| `X-Fs-Signature` | `hex(hmac_sha256(METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + BODY, FS_LMS_AD_HMAC_SECRET))` |

`PATH` — путь без хоста и query, `BODY` — сырые байты тела (у `GET` пусто). Расхождение времени
больше `HMAC_MAX_SKEW_SECONDS` (300) или неверная подпись → `401 {"error": "bad signature"}`,
запрос не обрабатывается. Реализация — `src/auth.py`; на сайте тот же расчёт делает
`AdRequestSigner`, эталонный вектор у обеих сторон общий.

Ручной вызов (например, проверка после настройки роутера):

```bash
SECRET=...; BODY=''; TS=$(date +%s)
SIG=$(printf 'GET\n/v1/health\n%s\n%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)
curl --cacert config/tls/server.crt https://92.101.127.156:8443/v1/health \
  -H "X-Fs-Timestamp: $TS" -H "X-Fs-Signature: $SIG"
```

С сайта то же самое — кнопка «Проверить соединение» или `wp fs-lms ad health`.

### `POST /v1/jobs`

Одно задание, ответ синхронный.

```json
{ "id": 12, "event": "provision", "idempotency_key": "app:5", "username": "i.petrov",
  "password": "…", "first": "Иван", "last": "Петров", "subject_key": "inf_ege" }

{ "id": 13, "event": "deprovision", "idempotency_key": "deprovision:person:7", "username": "i.petrov" }

{ "id": 14, "event": "password", "idempotency_key": "password:person:7:1790590811",
  "username": "i.petrov", "password": "…" }
```

`provision` приходит и при повторном зачислении (`idempotency_key: reenroll:record:{id}`) — учётка
из «Отчисленных» реактивируется и переносится в OU направления. `password` только меняет пароль:
нет учётки или она вне зоны — `failed`.

| Ответ | Когда |
|---|---|
| `200 {"status": "done", "error": null, "outcome": "created"}` | выполнено (в т.ч. повтор уже выполненного — операции идемпотентны) |
| `200 {"status": "failed", "error": "…"}` | ошибка задания: вне управляемой зоны, не подобрать CN, ошибка LDAP-операции |
| `422 {"status": "failed", "error": "невалидное задание"}` | тело не прошло валидацию (`job_invalid` в лог) |
| `503 {"error": "контроллер домена недоступен"}` | DC недоступен и после переподключения — не ошибка задания |
| `401` | подпись |

Сайт трактует `failed`/`422` как неудачную попытку (ретрай с бэкоффом, 6 неудач — «мёртвое»),
а `503`/`401`/нет связи — как недоступность офиса (пауза без траты попыток).

### `POST /v1/reconcile`

```json
{ "usernames": ["i.petrov", "a.ivanova"], "apply": false }
```

```json
{ "status": "ok", "applied": false, "disabled": ["x.old"], "abort_reason": null }
{ "status": "aborted", "applied": true, "disabled": [], "abort_reason": "к отключению 12 учёток, порог 10" }
```

`disabled` — отключённые (при `apply: false` — кого отключил бы, AD не меняется). Предохранители
(`RECONCILE_MAX_DISABLE`, `RECONCILE_MAX_DISABLE_PCT`, пустой список при непустой зоне) отменяют
сверку целиком в обоих режимах; учётки моложе `RECONCILE_GRACE_MINUTES` не трогаются. Невалидное
тело — `422`, DC недоступен — `503`.

### `GET /v1/health`

`200 {"status": "ok", "ldap": "ok"}` — сервис жив и DC отвечает; `503` — DC недоступен.

Документация FastAPI (`/docs`, `/openapi.json`) на публичном API выключена.

---

## 2. Локальный API

### `GET /health`

Docker HEALTHCHECK (`docker-compose.yml`).

```bash
curl -s http://localhost:8091/health
```

```json
{ "status": "ok", "last_job_at": "2026-09-28T10:20:11Z", "last_reconcile_at": null }
```

### `GET /status`

Счётчики журнала за всё время + последние 20 записей (без паролей и payload).

```json
{
  "done": 41, "failed": 2, "dead": 0,
  "recent": [
    { "job_id": 13, "idempotency_key": "deprovision:person:7", "event": "deprovision",
      "username": "i.petrov", "subject_key": null, "status": "done", "error": null,
      "received_at": "…", "acked_at": "…" }
  ]
}
```

`acked_at` — момент ответа сайту (имя поля осталось со времён pull-модели). `dead` — число
`idempotency_key` с 6 и более неудачами.

Внеочередной сверки на локальном API больше нет: список активных логинов есть только у сайта —
запуск вручную `wp fs-lms ad reconcile [--apply]`.
