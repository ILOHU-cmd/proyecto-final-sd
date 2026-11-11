# syntax=docker/dockerfile:1

# Imagen base pequeña y oficial. Se fija la versión menor para que el build
# sea reproducible entre los integrantes del grupo.
FROM python:3.12-slim

# - PYTHONDONTWRITEBYTECODE: no ensuciar la imagen con archivos .pyc.
# - PYTHONUNBUFFERED: los logs salen inmediatamente en docker compose logs.
# - DB_PATH: ruta por defecto dentro del contenedor; el volumen se monta en /data.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/chat.db

WORKDIR /srv

# Optimización de caché de capas: las dependencias se copian e instalan antes
# que el código. Si solo cambia app/ o client/, Docker reutiliza esta capa y
# el build es mucho más rápido.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El código cambia con más frecuencia, por eso va después.
COPY app ./app
COPY client ./client
COPY scripts ./scripts

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

# Healthcheck sin instalar curl: se usa el propio Python de la imagen.
# PORT se lee en tiempo de ejecución para que el mismo Dockerfile sirva al
# servidor de grupo (8000) y al central de prueba (9000).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=2)"

# exec reemplaza al shell para que uvicorn reciba las señales de Docker
# (SIGTERM) y pueda cerrar ordenadamente.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
