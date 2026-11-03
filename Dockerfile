FROM python:3.10-slim

# Instalar dependencias del sistema operativo mínimas y necesarias
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar requerimientos primero
COPY requirements.txt .

# Actualizar pip, instalar PyTorch CPU (sin paquetes CUDA pesados) y el resto de dependencias en una sola capa limpia
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch==2.2.1+cpu torchaudio==2.2.1+cpu --extra-index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el script del backend
COPY tu_script.py .

# Crear el directorio temporal
RUN mkdir -p TEMP_PROCESAMIENTO

EXPOSE 5000

CMD ["sh", "-c", "${CONTAINER_CMD:-python tu_script.py}"]
