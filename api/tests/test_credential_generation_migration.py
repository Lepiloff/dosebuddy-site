"""Legacy credential transition on genuine Alembic schemas in scratch databases."""

import asyncio
import os
import sys
import uuid
from datetime import timedelta

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import ACCESS_TOKEN_TTL, SYNC_TOKEN_TTL, hash_refresh_token
from app.db.models import utcnow
from app.db.schema_check import _credential_problems
from app.main import create_app
from tests.conftest import FakeGoogle, FakeRedis, TEST_KEY, auth_header, make_settings
from tests.test_sync_publication_migration import _alembic, migration_db  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _schema_check(url):
    env = {**os.environ, "ENVIRONMENT": "local", "DATABASE_URL": url,
           "REDIS_URL": "redis://localhost:6379/0", "ENCRYPTION_KEY": TEST_KEY,
           "JWT_SECRET": "synthetic-migration-secret-123456"}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.db.schema_check", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()
    return output.decode()


@pytest.mark.parametrize("fail_once", [False, True])
async def test_upgrade_preserves_active_legacy_and_fences_currently_revoked(migration_db, fail_once):
    url, engine = migration_db
    await _alembic(url, "0021")
    account, active, revoked = (uuid.uuid4() for _ in range(3))
    refresh = "synthetic-revoked-refresh"
    active_refresh = "synthetic-active-legacy-refresh"
    now = utcnow()
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO accounts (id, google_sub, created_at) VALUES (:id, 'generation-migration', :now)"
        ), {"id": account, "now": now})
        for device_id, revoked_at in ((active, None), (revoked, now)):
            await conn.execute(text(
                "INSERT INTO devices (id, account_id, platform, created_at, revoked_at) "
                "VALUES (:id, :account, 'android', :now, :revoked)"
            ), {"id": device_id, "account": account, "now": now, "revoked": revoked_at})
        await conn.execute(text(
            "INSERT INTO refresh_tokens (id, device_id, token_hash, expires_at, created_at, revoked_at) "
            "VALUES (:id, :device, :hash, :expiry, :now, :now)"
        ), {"id": uuid.uuid4(), "device": revoked, "hash": hash_refresh_token(refresh),
            "expiry": now + timedelta(days=90), "now": now})
        await conn.execute(text(
            "INSERT INTO refresh_tokens (id, device_id, token_hash, expires_at, created_at) "
            "VALUES (:id, :device, :hash, :expiry, :now)"
        ), {"id": uuid.uuid4(), "device": active, "hash": hash_refresh_token(active_refresh),
            "expiry": now + timedelta(days=90), "now": now})
        if fail_once:
            await conn.execute(text("""
                CREATE FUNCTION reject_generation_upgrade() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic migration interruption'; END; $$
            """))
            await conn.execute(text("""
                CREATE TRIGGER reject_generation_upgrade BEFORE UPDATE ON devices
                FOR EACH ROW EXECUTE FUNCTION reject_generation_upgrade()
            """))
    if fail_once:
        await _alembic(url, "head", succeeds=False)
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0021"
            assert (await conn.execute(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                "AND column_name='credential_generation'"
            ))).scalar_one() == 0
            assert (await conn.execute(text("SELECT revoked_at FROM devices WHERE id=:id"),
                                      {"id": revoked})).scalar_one() == now
            await conn.execute(text("DROP TRIGGER reject_generation_upgrade ON devices"))
            await conn.execute(text("DROP FUNCTION reject_generation_upgrade()"))
    await _alembic(url, "head")
    assert "durable credential generations" in await _schema_check(url)
    async with engine.connect() as conn:
        assert await _credential_problems(conn) == []
        before = (await conn.execute(text(
            "SELECT id, credential_generation, revoked_at FROM devices ORDER BY id"
        ))).all()
        assert {row[0]: row[1] for row in before} == {active: 0, revoked: 1}
        assert set((await conn.execute(text("SELECT credential_generation FROM refresh_tokens"))).scalars()) == {0}
    await _alembic(url, "head")
    async with engine.connect() as conn:
        assert (await conn.execute(text(
            "SELECT id, credential_generation, revoked_at FROM devices ORDER BY id"
        ))).all() == before

    settings = make_settings(database_url=url)
    app = create_app(settings)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis, app.state.google_verifier = FakeRedis(), FakeGoogle()
    app.state.google_verifier.add("synthetic-google", "generation-migration")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        legacy = {}
        for device in (active, revoked):
            for scope, ttl in ((None, ACCESS_TOKEN_TTL), ("sync", SYNC_TOKEN_TTL)):
                claims = {"sub": str(account), "did": str(device),
                          "iat": int(now.timestamp()), "exp": int((now + ttl).timestamp())}
                if scope:
                    claims["scope"] = scope
                legacy[device, scope] = {"access_token": jwt.encode(claims, settings.jwt_secret, algorithm="HS256")}
                response = await client.get("/v1/sync/pull", headers=auth_header(legacy[device, scope]))
                assert response.status_code == (200 if device == active else 401)
        active_pair = await client.post("/v1/auth/refresh", json={"refresh_token": active_refresh})
        assert active_pair.status_code == 200, active_pair.text
        assert (await client.get("/v1/sync/pull", headers=auth_header(active_pair.json()))).status_code == 200
        new = await client.post("/v1/auth/google", json={
            "id_token": "synthetic-google", "device": {"id": str(revoked), "platform": "android"},
        })
        assert new.status_code == 200
        for scope in (None, "sync"):
            assert (await client.get("/v1/sync/pull", headers=auth_header(legacy[revoked, scope]))).status_code == 401
            assert (await client.get("/v1/sync/pull", headers=auth_header(legacy[active, scope]))).status_code == 200
        assert (await client.post("/v1/auth/refresh", json={"refresh_token": refresh})).status_code == 401
        assert (await client.post("/v1/auth/refresh", json={"refresh_token": new.json()["refresh_token"]})).status_code == 200
        # Legacy active devices cross the same durable logout boundary too.
        assert (await client.post("/v1/auth/logout", headers=auth_header(legacy[active, None]))).status_code == 204
        again = await client.post("/v1/auth/google", json={
            "id_token": "synthetic-google", "device": {"id": str(active), "platform": "android"},
        })
        assert again.status_code == 200
        for scope in (None, "sync"):
            assert (await client.get("/v1/sync/pull", headers=auth_header(legacy[active, scope]))).status_code == 401


async def test_empty_upgrade_passes_real_schema_check_and_matches_models(migration_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.db.session import Base

    url, engine = migration_db
    await _alembic(url, "head")
    assert "durable credential generations" in await _schema_check(url)
    async with engine.connect() as conn:
        differences = await conn.run_sync(lambda sync_conn: compare_metadata(
            MigrationContext.configure(sync_conn, opts={
                "compare_type": True,
                # Legacy migrations retain defaults that old ORM models supply
                # only in Python. Check this change's server defaults exactly;
                # the schema helper independently verifies both new columns.
                "compare_server_default": lambda context, inspected, metadata, *args:
                    None if metadata.name == "credential_generation" else False,
                "include_object": lambda obj, name, kind, reflected, compare_to: name != "alembic_version",
            }), Base.metadata,
        ))
        assert differences == []
