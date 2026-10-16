"""Authority high-water parity and SQL cost through real PostgreSQL HTTP paths."""

import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import sync
from app.core.security import read_access_token
from app.db.models import Device, DeviceReadiness, DoseEvent, Medication, Profile, Schedule, StockEvent, utcnow
from app.db.publication import NEXT_SERVER_SEQ, lock_publication
from app.services import alerts
from tests.conftest import auth_header
from tests.test_sync import _owner_with_data, _pair, push, stock
from tests.test_sync_publication import _pid, _waiting

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world(api, session, db_engine):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    dev = uuid.UUID(read_access_token(api.app.state.settings.jwt_secret, owner['access_token'])['did'])
    ids = dict(profiles=uuid.UUID(pid), medications=uuid.UUID(mid),
               schedules=uuid.UUID(sid), dose_events=uuid.UUID(did), stock_events=uuid.uuid4())
    await push(api, owner, stock_events=[stock(str(ids['stock_events']), mid)])
    await session.execute(update(Profile).where(Profile.id == ids['profiles'])
                          .values(owner_device_id=dev, server_seq=text(NEXT_SERVER_SEQ)))
    await session.commit()
    return SimpleNamespace(api=api, session=session, engine=db_engine, owner=owner, device=dev,
                           ids=ids, account=uuid.UUID(owner['account_id']))


async def _ready(w, pid, cursor, *, revision=None, device=None):
    profile = await w.session.get(Profile, pid, populate_existing=True)
    w.session.add(DeviceReadiness(device_id=device or w.device, profile_id=pid,
                                  revision=profile.server_seq if revision is None else revision,
                                  applied_cursor=cursor))
    await w.session.commit()


async def _read(w, endpoint='pull', owner=None, cursor=None):
    query = {'ready': '1'} if endpoint == 'pull' else {}
    if cursor is not None:
        query['cursor'] = cursor
    response = await w.api.get(f'/v1/sync/{endpoint}', params=query, headers=auth_header(owner or w.owner))
    assert response.status_code == 200, response.text
    return response.json()


@contextmanager
def _water_queries(engine):
    statements = []

    def observe(_conn, _cursor, sql, params, _context, _many):
        if 'UNION ALL' in sql and 'max(' in sql.lower():
            statements.append((sql, params))

    event.listen(engine.sync_engine, 'before_cursor_execute', observe)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', observe)


@pytest.mark.parametrize('entity,model', [
    ('profiles', Profile), ('medications', Medication), ('dose_events', DoseEvent),
    ('schedules', Schedule), ('stock_events', StockEvent),
])
@pytest.mark.parametrize('deleted', [False, True])
async def test_each_live_or_deleted_branch_controls_the_exact_ready_threshold(world, entity, model, deleted):
    w = world
    await w.session.execute(update(model).where(model.id == w.ids[entity]).values(
        server_seq=text(NEXT_SERVER_SEQ), deleted_at_ms=999 if deleted else None))
    await w.session.commit()
    highest = (await w.session.get(model, w.ids[entity], populate_existing=True)).server_seq
    pid = w.ids['profiles']
    await _ready(w, pid, highest - 1)
    for endpoint in ['preview', 'pull']:
        page = await _read(w, endpoint)
        assert page['authority'][str(pid)]['owner_ready'] is False
        assert page['authority'][str(pid)]['owner_device_id'] == str(w.device)
        row = next(row for row in page['changes'][entity] if row['id'] == str(w.ids[entity]))
        assert row['deleted_at'] == (999 if deleted else None)
    await w.session.execute(update(DeviceReadiness).values(applied_cursor=highest))
    await w.session.commit()
    for endpoint in ['preview', 'pull']:
        assert (await _read(w, endpoint))['authority'][str(pid)]['owner_ready'] is True


@pytest.mark.parametrize('entity,model', [('schedules', Schedule), ('stock_events', StockEvent), ('dose_events', DoseEvent)])
async def test_a_deleted_medication_does_not_hide_a_newer_child(world, entity, model):
    w = world
    await w.session.execute(update(Medication).where(Medication.id == w.ids['medications'])
                          .values(deleted_at_ms=999, server_seq=text(NEXT_SERVER_SEQ)))
    await w.session.execute(update(model).where(model.id == w.ids[entity])
                          .values(server_seq=text(NEXT_SERVER_SEQ)))
    await w.session.commit()
    highest = (await w.session.get(model, w.ids[entity], populate_existing=True)).server_seq
    await _ready(w, w.ids['profiles'], highest - 1)
    assert (await _read(w))['authority'][str(w.ids['profiles'])]['owner_ready'] is False
    await w.session.execute(update(DeviceReadiness).values(applied_cursor=highest))
    await w.session.commit()
    assert (await _read(w, 'preview'))['authority'][str(w.ids['profiles'])]['owner_ready'] is True


async def _add_profile(w):
    row = Profile(id=uuid.uuid4(), owner_account_id=w.account, owner_device_id=w.device,
                  name='Synthetic', created_at_ms=1, updated_at_ms=1)
    w.session.add(row)
    await w.session.commit()
    return row


async def test_only_matching_current_owner_reports_need_high_water(world):
    w = world
    rows = [await w.session.get(Profile, w.ids['profiles'], populate_existing=True)]
    rows += [await _add_profile(w) for _ in range(4)]
    other = Device(id=uuid.uuid4(), account_id=w.account, platform='android', last_seen_at=utcnow())
    w.session.add(other)
    await w.session.commit()
    frontier = sync.decode_cursor((await _read(w))['cursor'])
    await _ready(w, rows[0].id, frontier)
    await _ready(w, rows[1].id, 0)  # Eligible report, but behind its watermark.
    # rows[2] has no report; rows[3] has a stale revision, rows[4] a wrong device.
    await _ready(w, rows[3].id, frontier, revision=rows[3].server_seq - 1)
    await _ready(w, rows[4].id, frontier, device=other.id)
    with _water_queries(w.engine) as queries:
        page = await _read(w)
    eligible = {rows[0].id, rows[1].id}
    bound_ids = {arg for _, params in queries for arg in params if isinstance(arg, uuid.UUID)}
    assert bound_ids == eligible
    assert {pid for pid, value in page['authority'].items() if value['owner_ready']} == {str(rows[0].id)}
    assert set(page['authority']) == {str(row.id) for row in rows}


@pytest.mark.parametrize('state', ['absent', 'old_revision', 'wrong_device'])
async def test_ineligible_reports_issue_no_high_water_query(world, state):
    w = world
    row = await w.session.get(Profile, w.ids['profiles'], populate_existing=True)
    frontier = sync.decode_cursor((await _read(w))['cursor'])
    if state == 'old_revision':
        await _ready(w, row.id, frontier, revision=row.server_seq - 1)
    elif state == 'wrong_device':
        other = Device(id=uuid.uuid4(), account_id=w.account, platform='android', last_seen_at=utcnow())
        w.session.add(other)
        await w.session.commit()
        await _ready(w, row.id, frontier, device=other.id)
    with _water_queries(w.engine) as queries:
        page = await _read(w, 'preview')
    assert queries == []
    assert page['authority'][str(row.id)]['owner_ready'] is False


async def test_empty_and_watcher_reads_have_no_authority_or_high_water(world):
    w = world
    watcher = await _pair(w.api, w.owner, str(w.ids['profiles']), role='viewer')
    with _water_queries(w.engine) as queries:
        page = await _read(w, owner=watcher)
    assert queries == [] and page['authority'] == {}
    assert page['roles'] == {str(w.ids['profiles']): 'viewer'}
    assert 'schedules' not in page['changes'] and 'stock_events' not in page['changes']
    assert all('owner_device_id' not in row for row in page['changes']['profiles'])
    assert all('dosage_text' not in row for row in page['changes']['medications'])
    # A fresh account with no membership is a genuinely empty authority read.
    from tests.conftest import sign_in
    empty = await sign_in(w.api, f'empty-{uuid.uuid4()}')
    with _water_queries(w.engine) as queries:
        page = await _read(w, owner=empty)
    assert queries == [] and page['authority'] == {} and page['roles'] == {}


async def test_preview_keeps_device_ack_and_stale_readiness_keeps_lease_metadata(world):
    w = world
    w.api.app.state.settings.authority_lease_minutes = 15
    await w.session.execute(update(Profile).where(Profile.id == w.ids['profiles']).values(authority_leased=True))
    await w.session.commit()
    await _ready(w, w.ids['profiles'], 0)
    ready = await w.session.get(DeviceReadiness, (w.device, w.ids['profiles']), populate_existing=True)
    dev = await w.session.get(Device, w.device, populate_existing=True)
    before_ack = (dev.cursor_seq, dev.ready_protocol_at)
    before = utcnow()
    page = await _read(w, 'preview')
    after = utcnow()
    authority = page['authority'][str(w.ids['profiles'])]
    assert authority['owner_ready'] is False
    assert datetime.fromisoformat(authority['ready_until'].replace('Z', '+00:00')) == ready.updated_at + timedelta(minutes=15)
    assert before <= datetime.fromisoformat(authority['as_of'].replace('Z', '+00:00')) <= after
    dev = await w.session.get(Device, w.device, populate_existing=True)
    assert (dev.cursor_seq, dev.ready_protocol_at) == before_ack
    pulled = await _read(w)
    dev = await w.session.get(Device, w.device, populate_existing=True)
    assert dev.cursor_seq == sync.decode_cursor(pulled['cursor']) and dev.ready_protocol_at is not None


@pytest.mark.parametrize('endpoint', ['pull', 'preview'])
async def test_authority_read_keeps_publication_until_the_page_is_finished(world, monkeypatch, endpoint):
    w = world
    frontier = sync.decode_cursor((await _read(w))['cursor'])
    await _ready(w, w.ids['profiles'], frontier)
    entered, release = asyncio.Event(), asyncio.Event()
    original = sync._authority

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(sync, '_authority', paused)
    maker = async_sessionmaker(w.engine, expire_on_commit=False)
    reading = asyncio.create_task(_read(w, endpoint))
    writing = None
    async with maker() as writer:
        try:
            await asyncio.wait_for(entered.wait(), 5)
            writer_pid = await _pid(writer)

            async def mutate():
                await writer.execute(update(DoseEvent).where(DoseEvent.id == w.ids['dose_events'])
                                     .values(server_seq=text(NEXT_SERVER_SEQ)))
                await writer.commit()

            writing = asyncio.create_task(mutate())
            assert await _waiting(writing, w.engine, writer_pid), 'writer crossed the authority publication fence'
            release.set()
            page = await asyncio.wait_for(reading, 5)
            assert page['authority'][str(w.ids['profiles'])]['owner_ready'] is True
            await asyncio.wait_for(writing, 5)
        finally:
            release.set()
            tasks = [task for task in (reading, writing) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await writer.rollback()
    assert (await _read(w, endpoint))['authority'][str(w.ids['profiles'])]['owner_ready'] is False


async def test_cost_eligible_profiles_share_one_high_water_statement(world):
    w = world
    rows = [await w.session.get(Profile, w.ids['profiles'], populate_existing=True)]
    rows += [await _add_profile(w) for _ in range(3)]
    frontier = sync.decode_cursor((await _read(w))['cursor'])
    for row in rows:
        await _ready(w, row.id, frontier)
    with _water_queries(w.engine) as queries:
        page = await _read(w)
    assert set(page['authority']) == {str(row.id) for row in rows}
    assert all(value['owner_ready'] for value in page['authority'].values())
    assert len(queries) == 1, f'{len(rows)} eligible profiles issued {len(queries)} high-water statements'


async def test_cost_long_dose_history_reads_only_the_index_tip(world):
    w = world
    # The initial fixture already has one dose. Include tombstones in the
    # remaining history; a watermark must cover all of it without reading it.
    for offset in range(0, 9999, 1000):
        await w.session.execute(insert(DoseEvent), [dict(
            id=uuid.uuid4(), profile_id=w.ids['profiles'],
            medication_id=w.ids['medications'], created_at_ms=1, updated_at_ms=1,
            planned_at_ms=i + 1, status='taken', dose_amount=1.0,
            deleted_at_ms=2 if i % 7 == 0 else None,
        ) for i in range(offset, min(offset + 1000, 9999))])
    await w.session.commit()
    expected = (await w.session.execute(select(func.max(DoseEvent.server_seq)))).scalar_one()
    assert (await w.session.execute(select(func.count()).select_from(DoseEvent))).scalar_one() == 10000
    await w.session.rollback()
    async with w.engine.begin() as conn:
        await conn.exec_driver_sql('ANALYZE')
    profile = await w.session.get(Profile, w.ids['profiles'], populate_existing=True)
    await lock_publication(w.session)
    with _water_queries(w.engine) as queries:
        actual = await alerts.profiles_high_water(w.session, [profile])
    assert actual == {w.ids['profiles']: expected}
    assert len(queries) == 1
    await w.session.rollback()
    sql, params = queries[0]
    async with w.engine.connect() as conn:
        plan = (await conn.exec_driver_sql(
            'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, params,
        )).scalar_one()
    if isinstance(plan, str):
        import json
        plan = json.loads(plan)

    def scans(node):
        if node.get('Relation Name') == 'dose_events':
            yield node
        for child in node.get('Plans', []):
            yield from scans(child)

    dose_scans = list(scans(plan[0]['Plan']))
    assert dose_scans, plan
    visited = sum((node['Actual Rows'] + node.get('Rows Removed by Filter', 0))
                  * node['Actual Loops'] for node in dose_scans)
    assert visited == 1, f'dose MAX read {visited} rows from a 10000-row history: {dose_scans}'
