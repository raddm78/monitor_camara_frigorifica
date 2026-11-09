FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

ENV PORT=8080 \
    DATABASE_PATH=/app/data/sensor_data.db
EXPOSE 8080

# 1 worker (SQLite) con varios hilos
CMD gunicorn --chdir backend app:app --workers 1 --threads 4 --bind 0.0.0.0:${PORT}
