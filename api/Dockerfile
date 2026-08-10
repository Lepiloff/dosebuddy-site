# The box is a t4g.small, so this builds for arm64. python:3.12-slim is
# multi-arch, which is the only reason nothing here mentions the architecture.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/api

# Dependencies first so that editing application code does not re-resolve them.
# Prefer the lock file when it exists; fall back to the loose bounds so a fresh
# clone can still build before the first lock is generated.
COPY requirements.txt requirements.lock.tx[t] ./
RUN if [ -f requirements.lock.txt ]; then \
        pip install -r requirements.lock.txt; \
    else \
        pip install -r requirements.txt; \
    fi

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Not root. This process will hold article 9 health data, and there is no
# reason for a remote code path to start out owning the filesystem too.
RUN useradd --system --uid 10001 --no-create-home api \
    && chown -R api:api /srv/api
USER api

EXPOSE 8000

# One worker: nginx is in front, the box has 2 vCPU shared with Postgres and
# Redis, and the work is IO-bound. More workers here would mean more idle
# Postgres connections, not more throughput.
# --factory because app.main exposes create_app() rather than a module-level
# app; settings are read when the app is built, not when the module is imported.
CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
