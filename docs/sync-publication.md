# Публикация sync-изменений и восстановление (0021)

Читать перед изменением writers зеркальных таблиц, pull pagination, порядка
блокировок и применением миграции `0021`. Wire contract v1.1 сохранён. Здесь
описано локальное исправление AUD-001; production rollout ещё не выполнен.

## Гарантия и её предпосылки

Sequence упорядочивает выдачу номеров, а не commit. Раньше транзакция A могла
получить 6, B закоммитить 8, pull сохранить 8, после чего поздний commit A
исчезал из последующих pull. Сортировка SELECT и retry с тем же cursor этого
пропуска не восстанавливают.

`app/db/publication.py` задаёт общий transaction advisory lock в пространстве
двух integer PostgreSQL: `(1396985678, 1)`. На всех одиннадцати таблицах ниже
стоит безусловный `BEFORE INSERT OR UPDATE OR DELETE FOR EACH STATEMENT` trigger.
Он вызывает `sync_publication_lock()` до row/FK locks и вычисления sequence
defaults. `next_server_seq()` вызывает ту же функцию до выдачи номера, покрывая
явные allocations и defaults всех пяти зеркал. Lock живёт до commit/rollback.

Следующий seq writer не может получить номер или закоммититься, пока предыдущий
не завершён. Откат оставляет допустимые пробелы sequence: строк в этих позициях
нет. Обязательна конфигурация `INCREMENT 1 CACHE 1 NO CYCLE`: кешированные на
соединениях диапазоны могут вернуть меньший номер в более поздней транзакции
даже при transaction lock. `schema_check` проверяет конфигурацию sequence,
каждый mirror default, точную форму/активность/функцию statement triggers и тела
трёх функций. API engine явно использует `READ COMMITTED`.

`sync._page` берёт publication отдельным SQL statement **до** чтения visibility,
medication ids, feed keys, hydration и authority. Writers заблокированы, поэтому
последующие запросы видят согласованное committed состояние. Pull удерживает lock
до обновления `Device.cursor_seq`/capabilities и commit; preview освобождает его
явным read-only rollback до возврата ответа. Отдельный statement необходим:
snapshot, взятый до ожидания lock, мог бы оказаться старее committed frontier.
Вызов `_page` внутри транзакции с ранее взятым repeatable-read snapshot не поддержан.

Push также берёт publication до savepoints/canonical advisory locks и сохраняет
response cursor под этим lock **до** commit. Ответ отправляется только после
успешного commit; он не может случайно взять `last_value` следующего, ещё не
опубликованного writer после освобождения lock.

## Writers и порядок блокировок

| Источник / production path | Таблицы / публикация |
| --- | --- |
| ORM/default inserts и обычные SQL INSERT/UPDATE/DELETE | `profiles`, `medications`, `schedules`, `dose_events`, `stock_events`: statement fence и защищённые defaults. UPDATE зеркальных данных по-прежнему обязан запросить свежий `next_server_seq()`. |
| `sync.push` → `_upsert_statement` / `_upsert` | Все пять зеркал, включая schedule/dose tombstones и действия; явный publication до chunk/per-row savepoints, defaults и UPDATE expressions используют общий allocator. |
| Canonical-dose/supersede в `sync.push`, `_catch_up_on_declaring` | `dose_events` и `devices`; publication предшествует отсортированным per-profile canonical advisory locks, затем row/FK locks. |
| Pairing redeem → membership + `_resend_profile` | `pairing_codes`, `profile_memberships`, затем переиздание profile/medication/dose; каждый statement защищён, каждая переизданная строка получает свой номер. |
| Claim authority и `/ready` | `profiles`, `device_readiness`, затем возможная очередь alerts; локальный AUD-002 берёт publication до precondition reads и удерживает до общего commit. |
| Auth sign-in/refresh/dependency/logout/удаление аккаунта | `accounts`, `devices`, `refresh_tokens` и изменения profiles/cascade; guards работают также до row locks от ORM autoflush. Logout не захватывает Device перед ожиданием publication от pull. |
| Worker → `alerts.hand_back_unconfirmed` | Publication до lease selection, свежие authority/readiness/previous Device; commit даже пустого pass до delivery (AUD-002). |
| Worker retention → `retention.sweep` | DELETE из `profile_memberships`; новых mirror positions сейчас нет, но visibility не может измениться посреди page. |
| `sync.pull` / `sync.preview` | Явный publication перед всеми чтениями page; Device writes от pull находятся под тем же lock. |

Полный набор защищённых таблиц: `accounts`, `devices`, `refresh_tokens`,
`profiles`, `device_readiness`, `profile_memberships`, `pairing_codes`,
`medications`, `schedules`, `dose_events`, `stock_events`. Для новой таблицы или
явного `SELECT FOR UPDATE` нужно определить место в этом порядке. Обычный DML
защищён в БД, поэтому покрыты и альтернативные/default writers. Явные advisory/
row locks берут **после** publication, если транзакция затем может публиковать
или писать защищённые таблицы. Нельзя заранее выделять номер сырым `nextval`
в отдельной незащищённой транзакции или двигать sequence назад.

Порядок: publication → отсортированные canonical profile advisory locks →
обычные Device/parent/FK rows → alert rows. Пространства двух типов advisory
locks не пересекаются. Нельзя брать publication после `FOR UPDATE` alert delivery.

`alert_deliveries` намеренно исключена: worker `take` удерживает row во время
FCM, затем только читает eligibility/profile/device и сохраняет delivery, не пишет защищённые
таблицы. FCM не удерживает publication **непосредственно**. Но account/profile
cascade может ждать эту строку; конфликт queue тоже может задержать транзакцию.
`sync_publication_lock` задаёт transaction-local `lock_timeout = 5000ms` до
захвата lock, ограничивая каждое ожидание DB lock. Это новый предел отдельного
ожидания, а не SLA всей транзакции или сети. Под нагрузкой возможен timeout.

Timeout прерывает statement. Локальный AUD-007 возвращает для необработанного
SQLAlchemy `DBAPIError` с PostgreSQL SQLSTATE `55P03` существующий совместимый
`429 rate_limited` и `Retry-After: 5`. Общий extractor читает код из driver
exception и явной цепочки causes, никогда из текста. Production request
dependency явно откатывает DBAPI failure до exception handler/response;
закрытие request session освобождает locks. Остальные DBAPI/resource errors
сохраняют sanitised `500 internal_error`. Новый endpoint/payload/error code
не введён. Неподтверждённый batch остаётся для retry; 429 не является ack.
Обработанный внутри savepoint timeout по-прежнему возвращает per-record retry.
Savepoint handling в push сохраняет уже применённые соседние строки
и возвращает retry для неудачной. Worker сохраняет durable pending deliveries
и повторяет проход. Удаление аккаунта либо коммитится полностью, либо откатывается
и допускает повтор. Fixture с приостановленным FCM/cascade проверяет rollback,
освобождение publication и последующее успешное удаление. Ошибочная запись не
получает вымышленного success/acknowledgement.

## Ограниченные chunks для push (локальный AUD-007, 13.10)

`profiles`, `medications`, `schedules`, `stock_events` применяются последовательно
chunks максимум 100 уникальных ID под одним savepoint: SAVEPOINT → один multi-row
INSERT/ON CONFLICT → RELEASE. Shapes текущих сущностей используют менее 2500
bind parameters на chunk при лимите asyncpg 32767. Лимит push 1000, pool 5+5,
lock timeout 5s, общий publication и credential rechecks не менялись.

Один `_upsert_statement` обслуживает chunk и прежний `_upsert`: tuple LWW
`(updated_at_ms, origin_device_id, op_seq)`, immutable-parent triggers,
`set_once`, отдельный `next_server_seq()` каждой принятой строки сохраняются.
Если chunk даёт SQLAlchemy error, nested context сначала откатывает **весь**
chunk, затем выполняется исходный per-row savepoint/upsert с прежними
`rejected`/`retry` codes. Неудачная строка не отравляет валидных соседей;
sequence gaps от отката допустимы. Успешные строки подтверждаются лишь после
общего commit и cursor capture под fence.

Повтор ID разрывает chunk перед повтором, включая границу 100: PostgreSQL
не получает один конфликтующий ID дважды в одном statement, а tuple LWW и
DB trigger видят исходную последовательность записей. Preflight refusal тоже
сначала завершает предыдущий chunk, сохраняя порядок SQL failures/outcomes.
Profiles завершаются и ownership перечитывается до medications; medications
завершаются до child visibility/FK checks. Dose writes, canonical advisory
locks, key holders, supersede и declaration catch-up остаются per-row.
Большой dose-heavy или invalid-heavy batch всё ещё может быть дорогим.

Regression gate проверяет ограниченный SQL budget 1/10/200/1000 medications,
pagination всех accepted rows, частичный rollback/retry плохого stock FK,
duplicate ID с parent move на границах 0/99/100, parents/ownership/isolation,
readiness invalidation/full high-water и canonical supersede/set-once key.
Реальные push/ready lock timeouts проверяют 429/header, отсутствие ack,
rollback, повтор того же batch и refusal superseded claim.

### Fair before/after на parent 665706b

Один host probe запускает production ASGI app/engine (READ COMMITTED, pool 5+5)
на выделенной PostgreSQL fixture: 20 synthetic accounts/devices/profiles,
детерминированные UUID и одни shapes/order пакетов. Перед каждым source run
таблицы пересозданы из metadata; оба прогона без искусственного sleep в sync,
Google/FCM/телефонов/внешнего HTTP и параллельного DB test runner. Python 3.10.7
в host venv отличается от production Docker 3.12. Это одно короткое сравнение
13.10 на exact `665706be2f6343072864344e68fdb6e081237582` и локальном diff;
старый 4394067 baseline в app audit не заменён.

SQL statements на успешный medication push: batch 1 — 15→15; 10 — 42→15;
200 — 612→18; 1000 — 3012→42. Authentication/handler publication и credential
rechecks входят в счёт. 429/500 до полезной работы имеют 5 statements. Timings
ниже включают успешные и failed запросы; req/s — все завершённые HTTP requests
делённые на wall time burst, не transaction capacity. Drain/retry в эти timings
не входит. p95 показан лишь при 20 samples как эмпирическая порядковая статистика;
p99 и steady-state tails из такой выборки не оцениваются.

| Batch / clients | До 200/500 → после 200/429 | req/s до→после | p50 s до→после | max s до→после | p95 s до→после |
|---|---|---|---|---|---|
| 1 / 1 | 1/0 → 1/0 | 19.01→20.74 | 0.053→0.048 | 0.053→0.048 | — |
| 1 / 10 | 10/0 → 10/0 | 55.74→57.40 | 0.114→0.111 | 0.178→0.172 | — |
| 1 / 20 | 20/0 → 20/0 | 83.50→82.75 | 0.172→0.177 | 0.233→0.235 | 0.229→0.232 |
| 10 / 1 | 1/0 → 1/0 | 32.32→62.23 | 0.031→0.016 | 0.031→0.016 | — |
| 10 / 10 | 10/0 → 10/0 | 26.13→47.06 | 0.225→0.132 | 0.380→0.210 | — |
| 10 / 20 | 20/0 → 20/0 | 30.53→64.53 | 0.406→0.208 | 0.649→0.304 | 0.626→0.298 |
| 200 / 1 | 1/0 → 1/0 | 2.27→11.79 | 0.440→0.085 | 0.440→0.085 | — |
| 200 / 10 | 10/0 → 10/0 | 2.20→10.82 | 2.491→0.486 | 4.530→0.919 | — |
| 200 / 20 | 15/5 → 20/0 | 1.79→7.79 | 5.666→1.249 | 11.166→2.555 | 10.315→2.422 |
| 1000 / 1 | 1/0 → 1/0 | 0.28→1.57 | 3.620→0.639 | 3.620→0.639 | — |
| 1000 / 10 | 2/8 → 8/2 | 1.29→1.85 | 5.131→3.797 | 7.752→5.378 | — |
| 1000 / 20 | 4/16 → 18/2 | 1.35→1.85 | 8.883→5.672 | 14.772→10.791 | 10.973→10.156 |

Mixed burst: 20 клиентов, по 5 push200/pull/ready/logout разных accounts.
В обоих прогонах 10×200 и 10×204; req/s 4.94→16.84,
p50 1.866→0.706s, max 4.020→1.162s,
p95 4.016→1.158s. Ready использует настоящий полный pull
cursor/high-water после import, logout проверен последующим 401 старого JWT.
В load fixture readiness до burst не заполнена: расходы repeated ready-owned
high-water queries отдельно проверены regression, но эта таблица их sustained
стоимость не измеряет.

После каждого burst failed request сохраняет исходные IDs/payload и имеет
до 3 serial retry attempts. На after 4×429 (по 2 на 10×1000 и 20×1000) каждый ответ
имеет `rate_limited`/`Retry-After: 5`; повтор после 5s завершается 200 без row
retry/rejection. SELECT из отдельной сессии сверяет все expected IDs; обе
серии восстановили в каждом случае полный набор, включая 20000 строк последнего
burst. Это host proof сохранности synthetic rows, не модель Flutter restart,
сети или sustained recovery. Общий fence сохраняется; pool/resource failures
не превращаются в 429. Dose/fallback/inventory/per-profile high-water costs,
RTT, queue age, lock wait/hold, память и staging на фактическом сервере остаются
[отдельным gate](debts.md#sync-overload-и-capacity-после-локального-aud-007).

Evidence находится в private temp root
`/tmp/sonadose-audit-remediation-20261013-_k2op4b8`:
`aud007-red.log/.xml` (final exact parent: 7 failures/10 passes, 0 errors),
`aud007-narrow.log/.xml` (15 new tests), `aud007-green.log/.xml` (276 related),
`aud007-load-before.log/.json`, `aud007-load-after.log/.json` и source
`aud007-load.py`; machine summary/полный diff — `aud007-evidence.json` и
`aud007-review.patch`. Initial red с ошибочной raw bytea name fixture хранится
отдельно и не используется как final proof. Scoped gate:

```bash
cd api
PYTHONDONTWRITEBYTECODE=1 TEST_DATABASE_URL='<private audit fixture URL>' \
  .venv/bin/python -m pytest \
  tests/test_sync_batching.py tests/test_sync.py \
  tests/test_sync_publication.py tests/test_authority_atomicity.py \
  tests/test_auth.py tests/test_credential_generation.py \
  tests/test_pairing.py tests/test_health.py -q
```

Root final review не нашёл blockers в production diff. Первый root full suite:
435 tests / 434 passed / 1 failure / 0 errors, 184.410s. Единственный failure —
старое ожидание 500 в `test_timed_out_revoke_does_not_ack_or_partially_end_consent`.
Review correction меняет только status/code на 429/rate_limited и проверяет
`Retry-After: 5`; durable consent/pending, no-ack, release publication во время
paused send и успешный повтор revoke assertions сохранены. Настоящий in-flight
PostgreSQL timeout regression — 1 passed за 5.699s; весь eligibility file —
34 passed за 16.133s. Evidence: `aud007-eligibility-narrow.log/.xml`,
`aud007-eligibility-green.log/.xml`; первый root suite сохранён отдельно как
`final-backend-suite-initial.log/.xml`. После correction root повторно одобрил
review; итоговый full backend suite — **435 passed**, failures/errors/skips0,
184.651s, exit0. Evidence: `final-backend-suite.log/.xml`; команда из api —
`.venv/bin/python -m pytest -q` с явным TEST_DATABASE_URL private fixture и
PYTHONDONTWRITEBYTECODE=1. После green менялись только эти документы. Deploy,
production/runtime capacity, реальные Google/FCM и устройства здесь не проверены.

## Атомарность authority transitions (AUD-002)

Claim, ready/renew, lease handback, logout и account erasure используют один
publication lock. Для переходов authority он берётся до precondition reads
и удерживается до commit. Account/Device/Profile перечитываются принудительно:
READ COMMITTED обновляет SQL snapshot, но не ORM identity map. Claim/ready
повторно проверяют eligibility после ожидания, поскольку authentication мог
завершиться раньше. Локальный AUD-003 также проверяет поколение исходного Caller
после lock, включая logout→login во время ожидания; подробности —
[архитектура токенов](sync-architecture.md#8-токены).

Если ready получил lock первым, следующий claim ждёт, затем сохраняется после
handover. Если новый claim уже committed, ready вытесненной заявки получает 409
и не очищает её. Child writes также ждут publication: high-water check и запись
readiness/authority проходят согласованно. Это не запрещает новые изменения
после commit и не доказывает состояние alarms на устройстве.

Legacy handover и durable nudge коммитятся вместе; ошибка очереди откатывает
переход. Точный applied cursor не повышается; второй ready после получения
handover остаётся обязательным для nudge gate. No-op claim и пустой lease pass
явно завершают транзакцию. Worker освобождает publication до AlertDelivery
FOR UPDATE и FCM. При timeout применяются те же rollback/retry правила выше.

Проверены 36 PostgreSQL scenarios (HTTP и production functions), включая
cached ORM, оба порядка ready/claim, child writes, lease renewal/handback,
logout/delete, timeout/retry, queue failure и configured lease→paused fake FCM.
Связанный regression gate — 248 passed; на approved001 source получены
ожидаемые red. Это host proof, не выполненный production/device handover.

### Диагностика возврата lease (локальный AUD-010, 13.10)

`worker.main` передаёт настроенный lease в `scan_once`, который до detection
вызывает `alerts.hand_back_unconfirmed`. При возврате B→A исходный owner теперь
сохраняется до ORM UPDATE: SQLAlchemy синхронизирует загруженный `Profile`, и
чтение его owner после UPDATE раньше давало warning A→A. Меняется только
`authority.handed_back.from_device_id`; authority SQL, guards, publication lock,
commit и настройки lease прежние. Warning по-прежнему пишется до commit.

Regression `test_worker_logs_the_expired_lease_owner_before_handback` запускает
настоящий worker на PostgreSQL с просроченной readiness B. Он проверяет warning
B→A и новым SELECT из отдельной сессии owner=A/previous=B, новую revision,
pending=NULL и leased=false. На parent `c8d616b` единственный red — неверное
поле source; узкий green — 1 passed, связанные `test_alerts.py`,
`test_authority_atomicity.py`, `test_sync_publication.py` — 97 passed.
Это host evidence: production config/deploy, естественный client lease/renew,
FCM и телефоны этим исправлением не проверялись.

## Восстановление ранее пропущенных строк

Миграция `0021` устанавливает функции/fences/defaults и переиздаёт **все сохранённые
строки пяти зеркал** одной транзакцией. Начальный frontier — максимум sequence
`last_value` и сохранённых mirror positions; все новые номера выше него.
Корректные старые cursors, выданные сервером, не могут превышать frontier,
включая номера, потраченные откатившимися транзакциями.

Device/readiness cursors не считаются доверенным входом восстановления: pull
может вернуть присланный cursor, ready принимает applied cursor. Подставленный
максимум bigint не должен исчерпать общую sequence. Произвольные cursors выше
действительно выданного сервером frontier не покрыты гарантией восстановления.

Меняется только `server_seq`. Сохраняются IDs, encrypted bytes, domain/LWW times,
origin/op_seq, soft-delete tombstones, canonical dose keys, `superseded_by` и
adoption/device state. Старые Android-клиенты получают идемпотентные повторы через
прежний wire. Переизданный profile получает новую revision: прежний readiness
устаревает до нового pull/ready; owner и pending claim не меняются. Authority
nudges временно удерживаются. Миграция не создаёт readiness за устройство.

Ошибка миграции откатывает схему, domain rows и Alembic revision; sequence
allocations нетранзакционны и могут оставить безвредные пробелы. Retry переиздаёт
строки от нового текущего frontier. Повторный `upgrade head` после успеха — no-op.
Regression fixtures обновляют настоящую схему 0020 с пропущенным поздним schedule
tombstone, завышают Device/readiness cursors, прерывают переиздание последней
таблицы, повторяют upgrade и проверяют сохранность данных и восстановление cursor.

<a id="caregiver-consent-retirement"></a>
## Завершение caregiver consent (AUD-006, 0023)

Общий query-builder задаёт eligibility detection и свежего dispatch: живое
`with_alerts`, неудалённый профиль, живые recipient и owner accounts.
Caregiver `claim` берёт publication **до** проверки/insert, чтобы stale detection
не создала новый intent после revoke. Worker завершает detection transaction
до delivery lock/FCM. Dispatch под `AlertDelivery FOR UPDATE SKIP LOCKED`
делает свежий SQL `EXISTS` без lifecycle locks и ORM snapshot; неeligible
intent становится durable `expired`. Authority nudge сохраняет прежний путь.

`0023` и model metadata содержат одинаковые AFTER ROW guards; миграция хранит
замороженный SQL. Guard работает на фактическом eligible→ineligible переходе
membership, живом membership DELETE и profile/account soft delete. Удаление
старого revoked history не отменяет новое live membership. Hard profile/account
DELETE ждёт delivery row при каскаде и удаляет её. Отменённый pending unique
intent остаётся завершённым при regrant. Upgrade также завершает уже сохранённые
неeligible caregiver debts, сохраняя sent и authority rows. `schema_check`
проверяет guards, условия, активность и полное тело функции.

Порядок revoke/downgrade/delete: publication → lifecycle rows → pending
delivery rows → commit/ack. Worker: delivery row → eligibility read → send →
delivery commit, **без** publication/membership lock после delivery lock.
Lifecycle writer может ждать уже начатый send; send завершится до successful
commit/ack. После acknowledgement новый send отменённого intent не начинается.
FCM, уже принявший сообщение, всё ещё может доставить его позже. DB wait
ограничен прежними 5 s: timeout откатывает lifecycle transaction и не выдаёт
success; HTTP revoke rollback/retry проверен на настоящей PostgreSQL.

Host tests используют настоящий HTTP/worker/PostgreSQL, приостановленный fake
sender, оба порядка queue/revoke, retry/no-token/regrant и настоящую0022
migration fixture с interrupted cleanup/rollback/retry. FCM и Android local
role/notification поведение требуют отдельного live gate; deploy не выполнен.

Проверено13.10: на approved`67264b5` все семь финальных red scenarios падают
по ожидаемой eligibility/retirement причине; scoped gate (worker, pairing,
auth/account, publication, authority, schema, retention, migration) —
**209 passed**. Empty upgrade→head/schema_check, точное metadata/migration
сравнение **38 indexes / 23 triggers / 5 functions**, downgrade→base — PASS.
Это synthetic host PostgreSQL evidence, без production capacity обещаний.

<a id="first-rollout-requires-a-maintenance-window"></a>
## Первый rollout требует окна обслуживания

Текущий `deploy/remote-deploy.sh` собирает образы, запускает `alembic upgrade head`
и `schema_check`, **затем** пересоздаёт API/worker. Во время миграции старые
контейнеры продолжают обслуживать запросы. Этот обычный порядок additive migration
не подходит для первого 0021: старые readers не берут page fence, а старый порядок
canonical locks не должен пересекаться с новым кодом/схемой.

До автоматического deploy, впервые содержащего 0021, 0022 или 0023, нужны проверенная
revision/image и явное окно обслуживания. Даже если 0021 уже установлен,
первый 0022 требует остановки старых API/issuers/worker: старый auth-код
не проверяет поколения и не должен работать с новой схемой. Первый0023 также
требует остановки прежнего worker: он не проверяет consent при dispatch.
Из `/home/ubuntu/dosebuddy-site/deploy` владелец должен
остановить **все** экземпляры API/worker и применить проверенный образ:

```bash
docker compose stop api worker
BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose build api worker
docker compose run --rm -T api alembic upgrade head
docker compose run --rm -T api python -m app.db.schema_check
docker compose up -d api worker
```

Первый upgrade нельзя выполнять обычным `remote-deploy.sh` при работающих старых
readers/writers. При ошибке upgrade/check оставить их остановленными, устранить
ошибку и повторить на проверенном коде. После запуска пройти существующую
приёмку из `deploy/README.md`. Откат, удаляющий publication protections, возвращает
риск потери cursor; downgrade не отменяет уже потреблённые клиентами позиции.
Удаление credential generations или запуск прежнего auth-кода теряет защиту
AUD-003 и может снова разрешить прежние JWT. Legacy-переход 0022 описан в
[архитектуре токенов](sync-architecture.md#8-токены).

Команды описывают будущую работу владельца. Deploy, production migration,
проверки живой нагрузки и Android alarms для этого локального исправления не
выполнялись. Локальное review пройдено; maintenance rollout, доставка изменений
между реальными клиентами и recovery/readiness rehearsal остаются отдельными gates.
