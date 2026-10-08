FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Seoul

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 engine && mkdir -p /data /state && chown engine /data /state
USER engine
# /state holds the cached KIS access token; keep it on a volume so restarts reuse it.
ENV KRX_DATA_DIR=/data KRX_STATE_DIR=/state
VOLUME ["/data", "/state"]

ENTRYPOINT ["/app/.venv/bin/krx-quant"]
CMD ["collect"]
