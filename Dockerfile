FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY auto_updater.py channels.py logging_setup.py main.py parsers.py risk.py settings.py \
     signal_parser.py stats.py status.py synctime.py tg_bot.py \
     trade_engine.py webapp.py /app/
COPY web /app/web

# Код и зависимости неизменяемы. Все рабочие файлы создаются в /data,
# который Docker Compose привязывает к папке проекта на флешке/диске.
WORKDIR /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 8080), 4); s.close()"

# В Compose исходники примонтированы в /data. Так проверка GitHub может
# обновить их до импорта модулей; /app остаётся резервной копией из образа.
CMD ["python", "/data/main.py"]
