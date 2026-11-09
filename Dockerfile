FROM python:3.11-slim

WORKDIR /app

# pypi.org иногда отвечает медленно с нод — увеличиваем таймаут и число повторов pip
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 PYTHONUNBUFFERED=1

COPY requirements.txt .
# Убираем BOM и CRLF из requirements.txt (файл редактировался в Windows)
RUN sed -i 's/^\xEF\xBB\xBF//' requirements.txt || true && tr -d '\r' < requirements.txt > req.tmp && mv req.tmp requirements.txt \
    && pip install --no-cache-dir -r requirements.txt && pip cache purge || true

COPY . .

# Постоянные данные (БД, найденные и отключённые источники) живут в data/:
# пересборка Bothost сохраняет только эту папку, токен, env и домен.
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

# main.py — постоянный процесс: сбор каналов и бирж + очередь LLM + кнопки
CMD ["python", "-u", "main.py"]
