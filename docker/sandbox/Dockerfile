# syntax=docker/dockerfile:1.7

ARG BASE_REFERENCE=python@sha256:49f3bd3ed3a1c554a43f1e9d03ff303716afed6eda0f3e98c34cd976c8880cd6
FROM ${BASE_REFERENCE} AS builder

COPY inputs/ /tmp/pico-inputs/
RUN set -eux; \
    cd /tmp/pico-inputs; \
    sha256sum -c SHA256SUMS; \
    dpkg -i debs/*.deb; \
    tar -xzf uv/uv-aarch64-unknown-linux-gnu.tar.gz \
        --directory=/usr/local/bin --strip-components=1; \
    uv --version; \
    git --version; \
    rg --version

COPY pyproject.toml uv.lock /opt/pico-project/
RUN set -eux; \
    cd /opt/pico-project; \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_PYTHON_DOWNLOADS=never \
    uv lock --check --offline; \
    UV_PYTHON_DOWNLOADS=never \
    uv venv --no-project --python=/usr/local/bin/python3 /opt/pico-venv; \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    uv pip install \
        --python=/opt/pico-venv/bin/python \
        --no-build \
        --no-index \
        --find-links=/tmp/pico-inputs/wheels \
        iniconfig==2.3.0 \
        packaging==26.2 \
        pluggy==1.6.0 \
        pygments==2.20.0 \
        pytest==9.1.1 \
        ruff==0.15.20; \
    uv pip check --python=/opt/pico-venv/bin/python; \
    /opt/pico-venv/bin/python -m pytest --version; \
    /opt/pico-venv/bin/ruff --version; \
    rm -rf /opt/pico-project /tmp/pico-inputs /tmp/uv-cache

RUN set -eux; \
    groupadd --gid 10001 pico; \
    useradd --uid 10001 --gid 10001 --home-dir /home/pico \
        --no-create-home --shell /bin/sh pico; \
    ln -s /usr/local/bin/python3 /usr/bin/python3; \
    ln -s /usr/local/bin/python3 /usr/bin/python; \
    mkdir -p /home/pico /workspace; \
    chown 10001:10001 /home/pico /workspace

FROM scratch
COPY --from=builder / /

ENV HOME=/home/pico \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/opt/pico-venv/bin:/usr/local/bin:/usr/bin:/bin \
    PICO_SANDBOX=1 \
    PICO_WORKSPACE=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp

LABEL io.pico.sandbox.image-policy="1" \
    io.pico.sandbox.managed="true" \
    org.opencontainers.image.title="Pico Docker Sandbox" \
    org.opencontainers.image.version="d1"

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT []
CMD []
