FROM python:3.14.7-slim-trixie@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/home/fakturek/.local/bin:$PATH

RUN groupadd --system --gid 10001 fakturek \
    && useradd --system --uid 10001 --gid fakturek --create-home fakturek
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl fonts-dejavu-core libharfbuzz0b libharfbuzz-subset0 libjpeg62-turbo libopenjp2-7 libpango-1.0-0 libpangoft2-1.0-0 mariadb-client shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=fakturek:fakturek requirements.lock requirements.txt pyproject.toml alembic.ini ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY --chown=fakturek:fakturek fakturek ./fakturek
COPY --chown=fakturek:fakturek alembic ./alembic
COPY --chown=fakturek:fakturek templates ./templates
COPY --chown=fakturek:fakturek static ./static
COPY --chown=fakturek:fakturek docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod 0555 /app/docker-entrypoint.sh \
    && mkdir -p /app/var/pdfs /app/var/imports /app/var/logs \
    && chown -R fakturek:fakturek /app/var
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD curl -fsS http://127.0.0.1:8000/healthz/db || exit 1
CMD ["/app/docker-entrypoint.sh"]
