# Knowledge Assistant

AI-бекенд: користувачі завантажують документи й ставлять питання, відповідь приходить із цитатами джерел. Наскрізний проєкт курсу SkillBoost, віхи M0–M10 описані в `docs/capstone.md` репозиторію SkillBoost.

**Стан: M0 + M1 + M2 + M3.** Є шари `api → services → repositories`, конфіг із середовища, помилки RFC 7807, health (`/health/ready` перевіряє БД і Redis), ruff + mypy + pytest + pre-commit, CI. Дані в PostgreSQL + pgvector (міграції Alembic), кеш у Redis із захистом від cache stampede. Без `KA_DATABASE_URL` застосунок працює на даних у памʼяті. Автентифікація: JWT access + refresh з ротацією, ролі, rate limit, ідемпотентність (M2).

## Запуск

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # за потреби
docker compose up -d --wait       # PostgreSQL (pgvector) і Redis
export KA_DATABASE_URL=postgresql+psycopg://ka:ka-dev-only@127.0.0.1:5432/ka   # Windows: set ...
export KA_REDIS_URL=redis://127.0.0.1:6379/0
alembic upgrade head              # застосувати міграції
uvicorn app.main:app --reload     # http://localhost:8000/docs
```

## Перевірки (те саме робить CI)

```bash
ruff check . && ruff format --check .
mypy app tests
pytest -q                         # швидкі тести (без БД), інтеграційні пропускаються
KA_TEST_DATABASE_URL=postgresql+psycopg://ka:ka-dev-only@127.0.0.1:5432/ka KA_TEST_REDIS_URL=redis://127.0.0.1:6379/0 pytest -q   # усе, включно зі справжніми PostgreSQL і Redis
pre-commit install                # хуки перед кожним комітом
```

Інтеграційні тести ЗНИЩУЮТЬ дані у вказаній базі (роблять `alembic downgrade base`), не вказуй чужу.

## Структура

| Шлях | Роль |
|---|---|
| `app/api.py` | HTTP: розбір запиту, відповідь; логіки немає |
| `app/services.py` | бізнес-логіка, кидає `DomainError` |
| `app/repositories.py` | доступ до даних (інтерфейс `Protocol`, у M1 реалізація на PostgreSQL) |
| `app/errors.py` | єдиний формат помилок `application/problem+json` |
| `app/config.py` | налаштування лише з env (`KA_*`), секрети як `SecretStr` |
| `app/models.py`, `migrations/` | схема БД (SQLAlchemy) і міграції Alembic; тест порівнює їх між собою |
| `app/security.py` | паролі (scrypt із параметрами в записі), JWT (HS256 зафіксовано, iss/aud/exp) |
| `app/services.py` | `AuthService` (вхід, ротація refresh, відкликання), `DocumentService` |
| `app/ratelimit.py`, `app/idempotency.py`, `app/kv.py` | ліміти запитів, `Idempotency-Key`, інтерфейс сховища (Redis або памʼять) |
| `app/logging_utils.py` | маскування PII і секретів у логах |
| `app/files.py`, `app/storage.py` | тип файлу за вмістом, безпечне імʼя; сховище (локальне або S3, потоковий запис із лімітом) |
| `app/ingest.py`, `app/jobs.py`, `app/worker.py` | текст → чанки → embeddings → pgvector; черга ARQ, ретраї, dead letters, outbox |
| `app/cache.py` | кеш у Redis: замок `SET NX` проти stampede, TTL із jitter, деградація при збої Redis |

## Дані та індекси (M1)

Таблиці: `users`, `documents`, `chunks` (з вектором `embedding`), `chat_sessions`. Кожен індекс існує під конкретний запит, а `EXPLAIN` підтверджує, що його використано (2000 фрагментів, 20 000 документів, `ANALYZE`):

| Запит | Індекс | План |
|---|---|---|
| мої документи, нові першими: `WHERE owner_id = ? ORDER BY created_at DESC LIMIT 20` | `ix_documents_owner_created (owner_id, created_at)` | `Index Scan Backward`, без `Sort`, 0.1 мс |
| найближчі фрагменти за змістом: `ORDER BY embedding <=> $q LIMIT 5` | `ix_chunks_embedding_hnsw` (HNSW, косинус, `m=16`, `ef_construction=64`) | `Index Scan using ix_chunks_embedding_hnsw`, 1.3 мс |
| порядок фрагментів документа, захист від дублів | `uq_chunks_document_position (document_id, position)` | унікальне обмеження, покриває й пошук за документом |
| історія чатів користувача | `ix_chat_sessions_user_created (user_id, created_at)` | як у документів |

Перевірити самому: `EXPLAIN (ANALYZE, COSTS OFF) ...` у `psql`. На майже порожній таблиці планувач вибере `Seq Scan`: це нормально, тому тест примусово вимикає `enable_seqscan`.

### Міграції без простою

`0002_document_content_hash` це перша фаза **expand → backfill → contract**: колонка додається NULLABLE із `lock_timeout`. Заповнення й `NOT NULL` (через `CHECK ... NOT VALID` → `VALIDATE`) робляться окремими наступними міграціями між розгортаннями коду. Опис кроків у докстрингу самої міграції.

### Кеш і cache stampede

`Cache.get_or_compute(key, compute)`: у кеші є значення, тоді віддаємо його; ні, тоді один запит бере замок і рахує, інші чекають результат (до `wait_max`); TTL розкидається випадково; якщо Redis недоступний, запит просто рахується без кешу. Перевірено на 20 паралельних потоках: `compute` викликається один раз. До ендпоінтів кеш підключається в M4 (відповіді `/ask`), зараз це готовий і протестований модуль.

## Доступ і захист (M2)

- **Паролі:** `hashlib.scrypt` (N=2^15, r=8, p=1), параметри зберігаються в записі, при вході слабкий хеш автоматично перехешується. Довжина 12..128 символів (верхня межа проти DoS).
- **Токени:** access JWT на 15 хв (`HS256` зафіксовано, перевіряються `iss`, `aud`, `exp`), refresh це довгий випадковий рядок, у базі лише його SHA-256. Refresh **використовується один раз**: ротація; повторне використання старого токена закриває всю «родину» (ознака крадіжки). Термін за неактивністю 7 днів, абсолютна межа 30 днів (ротація її не продовжує). Токени повертаються в JSON і передаються заголовком `Authorization`, cookie немає, тому CSRF-захист не потрібен; якщо додаси cookie, то `HttpOnly`, `Secure`, `SameSite` і CSRF-токен (завдання №143 SkillBoost).
- **Ролі й власник:** `user` і `admin` (`/api/v1/admin/users` дає 403 звичайному користувачу). Чужий документ відповідає 404, як неіснуючий (без IDOR). Роль у access-токені діє до його спливу (15 хв), тож зміну ролі видно не одразу.
- **Rate limit (Redis):** вхід обмежується окремо на (IP, email) і на email, API на користувача; відповідь 429 із `Retry-After`. Збій Redis не вимикає API (fail-open); для входу можна обрати fail-closed. За reverse proxy IP треба брати з довіреного `X-Forwarded-For`, зараз береться адреса зʼєднання.
- **Ідемпотентність:** `POST /api/v1/documents` з `Idempotency-Key` не створює дубль при повторі; той самий ключ з іншим тілом це 422, ключ належить користувачу.
- **Логи:** фільтр маскує email, `Bearer`/JWT і `password=`/`token=` (`app/logging_utils.py`).
- **Секрети:** `KA_JWT_SECRET` лише з env; у `prod` без ≥32 символів застосунок не стартує. У CI `pip-audit` перевіряє залежності, `ruff` з правилами `S` (bandit).
- **Тест маршрутів:** `tests/test_units.py` обходить публічний OpenAPI: кожен маршрут або захищений, або в явному списку публічних, і анонімний запит до захищених дає 401 (аналог завдання №163 SkillBoost).

## Інгест документів (M3)

Потік: `POST /api/v1/documents` (метадані) → `PUT /api/v1/documents/{id}/file?filename=...` (сирі байти) → статус `queued` → воркер індексує → `indexed` або `failed`.

- **Завантаження:** тіло пишеться шматками, ліміт `KA_MAX_UPLOAD_BYTES` перевіряється на льоту (і за `Content-Length`); перевищення це 413, недописаний файл прибирається. Тип визначається за вмістом (PDF і текст UTF-8); виконувані файли, бінарні дані, порожні файли це 415, заявлений `Content-Type` і розширення ігноруються. У сховищі файл лежить під випадковим ключем, імʼя від клієнта лише очищений рядок для показу; ключ має суворий формат, тож вихід за межі теки неможливий.
- **Прямо в S3:** `POST /documents/{id}/upload-url` повертає presigned POST з умовою `content-length-range`, потім `POST /documents/{id}/complete` перевіряє розмір і тип за першими байтами й прибирає невідповідний файл. Для локальної розробки є MinIO у `docker-compose.yml` (образ `bitnamilegacy`, застарілий, лише dev).
- **Воркер:** `arq app.worker.WorkerSettings`. До 4 спроб із паузою 5, 10, 20... с; «вічні» помилки (битий PDF, немає тексту) одразу `failed`; після останньої спроби документ стає `failed`, а завдання пишеться в `dead_letters`. Індексація ідемпотентна (повтор не дублює чанки).
- **Outbox:** подія `document.indexed` пишеться в тій самій транзакції, що й чанки; cron-завдання воркера публікує непублікуване кожні 10 с (at-least-once, споживач має бути ідемпотентним).
- **Embeddings:** без `KA_LLM_BASE_URL`+`KA_EMBEDDING_MODEL` береться детермінований `HashEmbedder` (лексична схожість, лише dev і тести); інакше `POST {base}/embeddings` через ваш проксі.
- **Без Redis** (`KA_REDIS_URL` порожній) індексація йде одразу в процесі API (одна спроба).
- Завантаження за URL не реалізовано; якщо додаси, то із захистом від SSRF (завдання №58 SkillBoost).

Тести: `tests/integration/test_worker.py` (справжній ARQ, ретраї, DLQ, векторний пошук), `test_s3.py` (MinIO). Нюанс ARQ: `Worker` треба створювати всередині запущеного event loop, інакше завдання не виконуються; на Windows `Worker.close()` не працює (SIGUSR1).

## Далі

M4: `/ask` (hybrid search, цитати, SSE, структурований вивід) і підключення кешу. Правило: що вимагає віха, те й додається, не раніше.
