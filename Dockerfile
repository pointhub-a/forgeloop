FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m pip install "build>=1.2,<2" \
    && python3 -m build --wheel --outdir /wheels

FROM python:3.12-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 forgeloop \
    && useradd --system --uid 10001 --gid forgeloop --home-dir /nonexistent forgeloop \
    && mkdir -p /workspace /data /run/secrets \
    && chown forgeloop:forgeloop /workspace /data

COPY --from=builder /wheels /wheels
RUN python3 -m pip install /wheels/*.whl \
    && rm -rf /wheels \
    && chmod -R a-w /usr/local/lib/python3.12/site-packages/forgeloop*

VOLUME ["/workspace", "/data", "/run/secrets"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"]

USER forgeloop
WORKDIR /workspace
CMD ["forgeloop", "serve", "--provider", "demo", "--host", "0.0.0.0", "--port", "8000", "--allow-remote", "--allowed-host", "localhost", "--allowed-host", "127.0.0.1", "--data-dir", "/data"]
