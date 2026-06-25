FROM python:3.10-slim

# Instalar dependencias del sistema operativo (ffmpeg es requerido por MoviePy, libsndfile1 es requerido por soundfile)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

# Copiar e instalar requerimientos
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el script del backend
COPY tu_script.py .

# Crear el directorio temporal
RUN mkdir -p TEMP_PROCESAMIENTO

EXPOSE 5000

CMD ["sh", "-c", "${CONTAINER_CMD:-python tu_script.py}"]
