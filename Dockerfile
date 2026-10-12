FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Seoul

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

# LightGBM (Turing) needs the GNU OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# /archive is a mount point for a local Object Store (deploy/smoke-local.sh); the server uses S3.
RUN useradd --create-home --uid 10001 engine && mkdir -p /data /state /archive \
    && chown engine /data /state /archive
USER engine
# /state holds the cached KIS access token; keep it on a volume so restarts reuse it.
ENV KRX_DATA_DIR=/data KRX_STATE_DIR=/state
# The commit the image is built from (deploy/auto-update.sh passes it), last: it changes
# with every build and must not invalidate the layers above.
ARG KRX_COMMIT=unknown
ENV KRX_COMMIT=$KRX_COMMIT
VOLUME ["/data", "/state"]

ENTRYPOINT ["/app/.venv/bin/krx-quant"]
CMD ["collect"]
