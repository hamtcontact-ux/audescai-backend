FROM python:3.10-slim

# Instalar dependencias del sistema operativo mínimas y necesarias
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Actualizar pip para compatibilidad total con PEP 625 y paquetes modernos
RUN pip install --no-cache-dir --upgrade pip

# 2. Instalar PyTorch y TorchAudio versión CPU pura (ahorra más de 5GB de CUDA/NVIDIA que saturaban el disco)
RUN pip install --no-cache-dir torch==2.2.1+cpu torchaudio==2.2.1+cpu --extra-index-url https://download.pytorch.org/whl/cpu

# 3. Copiar e instalar el resto de dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el script del backend
COPY tu_script.py .

# Crear el directorio temporal
RUN mkdir -p TEMP_PROCESAMIENTO

EXPOSE 5000

CMD ["sh", "-c", "${CONTAINER_CMD:-python tu_script.py}"]
