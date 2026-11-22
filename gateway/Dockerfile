# Multi-arch (amd64 + arm64). Build context: ./gateway
FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/gateway.db
COPY --from=build /install /usr/local
RUN useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# One worker on purpose: the generation slot is held per process.
CMD ["uvicorn", "gyan_gateway.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
