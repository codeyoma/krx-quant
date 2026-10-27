FROM python:3.11-slim

# UV_NO_CACHE (#379): uv's download cache would stay in the layer beside the venv it fills
# (364 MB more in each dependency layer).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    TZ=Asia/Seoul

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

# LightGBM (Turing) needs the GNU OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# /archive is a mount point for a local Object Store (deploy/smoke-local.sh); the server uses S3.
RUN useradd --create-home --uid 10001 engine && mkdir -p /data /state /archive \
    && chown engine /data /state /archive

WORKDIR /app
# The dependencies, from pyproject.toml and the lock alone (#379): README.md, which most PRs
# change, was copied here too and rebuilt this layer (the whole venv) at nearly every deploy.
# Their bytecode is compiled here, once.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# The project on top, editable (its code stays in /app/src): a deploy that changes only the
# code or README.md adds these small layers. Nothing to compile: the dependencies' bytecode is
# above, and the project's own is not kept (PYTHONDONTWRITEBYTECODE), as before.
COPY README.md ./
COPY src ./src
RUN UV_COMPILE_BYTECODE=0 uv sync --frozen --no-dev

USER engine
# /state holds the cached KIS access token; keep it on a volume so restarts reuse it.
ENV KRX_DATA_DIR=/data KRX_STATE_DIR=/state
# The app's venv first on PATH (#322): `docker compose exec engine krx-quant <command>` finds the
# command, and `python` there is the app's (the image's own lacks its packages, e.g. pyarrow).
ENV PATH=/app/.venv/bin:$PATH
# The commit the image is built from (deploy/auto-update.sh passes it), last: it changes
# with every build and must not invalidate the layers above.
ARG KRX_COMMIT=unknown
ENV KRX_COMMIT=$KRX_COMMIT
VOLUME ["/data", "/state"]

ENTRYPOINT ["/app/.venv/bin/krx-quant"]
CMD ["collect"]
