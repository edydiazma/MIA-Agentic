FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --create-home --uid 10001 app

COPY app ./app
USER app

EXPOSE 8000
# /health/ready: base alcanzable, LISTEN entre réplicas conectado (api) y latido reciente (worker)
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"

# Un proceso por contenedor. La misma imagen corre con ROLE=api (HTTP + WebSocket, escalable a varias réplicas:
# los eventos en vivo viajan entre réplicas por Postgres LISTEN/NOTIFY) o ROLE=worker (tareas de fondo; cada
# tarea corre una sola vez en el clúster gracias a un advisory lock). docs/data-model.md §12.1
ENV ROLE=all
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
