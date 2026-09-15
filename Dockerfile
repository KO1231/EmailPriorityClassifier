# syntax=docker/dockerfile:1
#
# One image for every deployment. What differs between running this locally and
# running it on Fargate is environment variables and mounts, nothing else.
#
# Configuration, credentials and output are mounted rather than baked in:
# config.yml and policy.yml are personal, secrets are secret, and log/ and
# .state/ are written at run time.

# Pinned by digest: a tag is mutable, and a base image that changes under a
# build is a supply-chain hole of exactly the kind the lockfile closes.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
# uv is installed from PyPI rather than copied from its own image. That keeps
# the build to one registry and one credential path — and uv is a build-time
# tool that never reaches the runtime layer, while the supply chain that
# actually ships is pinned by uv.lock's per-package hashes.
ARG UV_VERSION=0.12.5

# ---------------------------------------------------------------------------
# builder — resolves and installs into a virtualenv that is copied out whole
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ARG UV_VERSION
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Dependencies before source, so editing code does not re-resolve them.
# `--frozen`: a lockfile that does not match pyproject.toml fails the build
# rather than being quietly resolved around.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra classify --extra aws

COPY src/ ./src/
COPY README.md ./
# `--no-editable`: uv installs the project as an editable link by default, which
# would point the runtime layer at a /build/src that does not exist there.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra classify --extra aws

# ---------------------------------------------------------------------------
# runtime — no compiler, no uv, no build cache
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    TZ=Asia/Tokyo

# A fixed UID so a bind-mounted log/ or .state/ has predictable ownership.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin epc

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# The generic prompt templates are part of the program. Personal rules are not:
# policy.yml is mounted, which is what lets these stay publishable.
COPY prompts/ /app/prompts/

# Mount points. Created here so a read-only root filesystem still has somewhere
# to write once these are mounted.
RUN mkdir -p /app/log /app/.state && chown -R epc:epc /app/log /app/.state

# /tmp as a declared volume, world-writable with the sticky bit. On Fargate, a
# task volume mounted at a path the image declares as a VOLUME is seeded from
# the image, permissions included. Mounted anywhere else it is root-owned 0755,
# which UID 10001 cannot write to — and a dry run on ECS writes its plan there.
RUN chmod 1777 /tmp
VOLUME ["/tmp"]

USER epc

# `epc` rather than a script path, so `docker run … run --dry-run` reads the
# same as the command you would type locally.
ENTRYPOINT ["epc"]
CMD ["run", "--dry-run"]
