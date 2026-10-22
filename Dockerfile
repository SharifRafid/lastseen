FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/lastseen ./src/lastseen
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --uid 1000 --create-home lastseen && mkdir /data && chown lastseen /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    LASTSEEN_DATA_DIR=/data \
    LASTSEEN_HOST=0.0.0.0 \
    LASTSEEN_PORT=8765 \
    LASTSEEN_IN_DOCKER=1 \
    LASTSEEN_CREDENTIAL_STORE=file \
    LASTSEEN_NO_BROWSER=1 \
    PYTHONUNBUFFERED=1
USER lastseen
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('LASTSEEN_PORT','8765'),timeout=3)"
CMD ["lastseen"]
