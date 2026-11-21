FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

RUN mkdir -p /app/data/reports /app/data/backups

EXPOSE 8000

# Runs FastAPI PWA server + built-in 24/7 60-minute autonomous background scheduler
CMD ["python", "-m", "backend.cli", "serve", "--host", "0.0.0.0", "--port", "8000", "--with-scheduler"]
