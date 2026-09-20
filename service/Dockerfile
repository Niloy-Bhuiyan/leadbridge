# Slim rather than alpine: the wheels for the dependency set are prebuilt for
# glibc, and musl would force a source build of several of them.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies are copied and installed before the source so an edit to the
# application does not invalidate the dependency layer on every rebuild.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app

# The database lives on a volume, not in the image layer.
ENV DATABASE_PATH=/data/leadbridge.db
RUN mkdir -p /data && useradd --create-home --uid 10001 app \
    && chown -R app:app /data /srv
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ops/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
