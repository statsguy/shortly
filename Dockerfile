# syntax=docker/dockerfile:1
#
# Two stages: a builder with uv that resolves and installs dependencies, and a runtime
# image that receives only the finished virtualenv. Build tools never reach production.
#
# Base images are pinned by digest (tags are mutable); Dependabot bumps the digests.
# Keep the two FROM lines identical so builder and runtime share one Python.

# ---------------------------------------------------------------------------- builder
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Layer 1: third-party dependencies only. This layer is rebuilt only when the lockfile
# changes, so editing application code doesn't reinstall FastAPI and friends.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Layer 2: the project itself, installed as a regular (non-editable) package.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------- runtime
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

# Fixed, non-root UID/GID: predictable file ownership and compatible with
# Kubernetes `runAsNonRoot`. No login shell, no home directory contents.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 # The base image ships pip (with its own vendored urllib3, msgpack, ...). The runtime
 # never installs anything, so pip is pure attack surface and scanner noise. Remove it.
 && python -m pip uninstall --yes --quiet pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip

WORKDIR /app

# Code stays owned by root and is only *readable* by `app`: a compromised process can't
# rewrite its own code or dependencies. (Hence no --chown here.)
COPY --from=builder /app/.venv /app/.venv
# Migrations ship in the same image so the schema and the code that expects it are
# versioned together. They run as a separate one-shot command, never on app startup.
COPY alembic.ini ./
COPY migrations ./migrations

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# For `docker run` / compose. Kubernetes ignores this and uses its own probes.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly, so in-flight requests
# finish before exit. (Shell form would put /bin/sh in front, which swallows signals.)
CMD ["uvicorn", "shortly.api:create_app_from_env", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "20", \
     "--no-server-header"]
