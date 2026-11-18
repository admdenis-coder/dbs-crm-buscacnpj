FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DBS_DATA_DIR=/data
WORKDIR /app
RUN groupadd --gid 10001 dbs && useradd --uid 10001 --gid dbs --no-create-home dbs && mkdir /data && chown dbs:dbs /data
COPY --chown=dbs:dbs dbs_crm ./dbs_crm
COPY --chown=dbs:dbs web ./web
USER dbs
EXPOSE 8765
CMD ["python", "-m", "dbs_crm.server", "--host", "0.0.0.0"]
