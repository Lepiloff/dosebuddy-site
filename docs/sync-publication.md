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
| `sync.push` → `_upsert` | Все пять зеркал, включая schedule/dose tombstones и действия; явный publication до savepoints, defaults и UPDATE expressions используют общий allocator. |
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
FCM, затем только читает profile/device и сохраняет delivery, не пишет защищённые
таблицы. FCM не удерживает publication **непосредственно**. Но account/profile
cascade может ждать эту строку; конфликт queue тоже может задержать транзакцию.
`sync_publication_lock` задаёт transaction-local `lock_timeout = 5000ms` до
захвата lock, ограничивая каждое ожидание DB lock. Это новый предел отдельного
ожидания, а не SLA всей транзакции или сети. Под нагрузкой возможен timeout.

Timeout прерывает statement. Обычная ошибка запроса приводит к rollback и
существующему ответу `500 internal_error`; неподтверждённый client batch остаётся
для retry. Savepoint handling в push сохраняет уже применённые соседние строки
и возвращает retry для неудачной. Worker сохраняет durable pending deliveries
и повторяет проход. Удаление аккаунта либо коммитится полностью, либо откатывается
и допускает повтор. Fixture с приостановленным FCM/cascade проверяет rollback,
освобождение publication и последующее успешное удаление. Ошибочная запись не
получает вымышленного success/acknowledgement.

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

<a id="first-rollout-requires-a-maintenance-window"></a>
## Первый rollout требует окна обслуживания

Текущий `deploy/remote-deploy.sh` собирает образы, запускает `alembic upgrade head`
и `schema_check`, **затем** пересоздаёт API/worker. Во время миграции старые
контейнеры продолжают обслуживать запросы. Этот обычный порядок additive migration
не подходит для первого 0021: старые readers не берут page fence, а старый порядок
canonical locks не должен пересекаться с новым кодом/схемой.

До автоматического deploy, впервые содержащего 0021 или 0022, нужны проверенная
revision/image и явное окно обслуживания. Даже если 0021 уже установлен,
первый 0022 требует остановки старых API/issuers/worker: старый auth-код
не проверяет поколения и не должен работать с новой схемой. Из `/home/ubuntu/dosebuddy-site/deploy` владелец должен
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
