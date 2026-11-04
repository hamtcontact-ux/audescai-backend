# -*- coding: utf-8 -*-
import os
import time
import base64
import json
import asyncio
import subprocess
import sys
import urllib.request
import uuid
import math
from functools import wraps

# Forzar actualización de dependencias críticas en el arranque
try:
    print("[Startup] Intentando forzar actualización de edge-tts a >=6.1.12...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "edge-tts>=6.1.12"])
    print("[Startup] Actualización de edge-tts completada exitosamente.")
except Exception as e:
    print(f"[Startup Warning] No se pudo forzar la actualización de edge-tts: {e}")

# Forzar actualización de redis a >=6.1.0 requerido por Celery/Kombu 5.3+
try:
    import redis
    parts = [int(p) for p in redis.__version__.split(".")[:2] if p.isdigit()]
    if not parts or parts[0] < 6 or (parts[0] == 6 and parts[1] < 1):
        print(f"[Startup] Redis actual ({redis.__version__}) es menor a 6.1.0. Actualizando a >=6.1.0...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "redis>=6.1.0"])
        import importlib
        importlib.reload(redis)
        print("[Startup] Redis actualizado exitosamente a >=6.1.0.")
except Exception as e_red:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "redis>=6.1.0"])
    except Exception:
        pass

# Configuración del entorno base
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CARPETA_AUDIOS = os.path.join(BASE_DIR, "TEMP_PROCESAMIENTO")
os.makedirs(CARPETA_AUDIOS, exist_ok=True)
TEMP_DIR = CARPETA_AUDIOS

# Carga de variables de entorno desde archivo .env local
def cargar_env():
    env_file = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
            print("[Env] Variables de entorno cargadas desde .env")
        except Exception as e:
            print(f"[Env Warning] Error al leer .env: {e}")

cargar_env()

# ==============================================================================
# CONFIGURACIÓN DEL NEGOCIO Y SUPERADMINISTRADOR ÚNICO
# ==============================================================================
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "editor.online.latam@gmail.com").strip().lower()

# Configuración OpenRouter (Modelos Gratuitos para rol 'free')
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/").strip()
OPENROUTER_FREE_MODEL = os.environ.get("OPENROUTER_FREE_MODEL", "gemini-3.5-flash-lite").strip()
OPENROUTER_FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "https://cineparaciegos.com").strip()
OPENROUTER_TITLE = os.environ.get("OPENROUTER_TITLE", "CineParaCiegos AudescAI").strip()

# Clave de OpenAI reservada para el servidor.
# NUNCA debe enviarse al frontend ni guardarse en el navegador del usuario.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Parámetros del Algoritmo de Rentabilidad (Calibrado con datos reales empíricos: 1.09 min = 78,803 tokens = $0.02 USD)
DEFAULT_TEXT_TOKENS_PER_MIN = int(os.environ.get("DEFAULT_TEXT_TOKENS_PER_MIN", "200"))
DEFAULT_VISION_TOKENS_PER_MIN = int(os.environ.get("DEFAULT_VISION_TOKENS_PER_MIN", "72000"))
TARGET_GROSS_MARGIN_PERCENT = float(os.environ.get("TARGET_GROSS_MARGIN_PERCENT", "50.0"))
API_COST_PER_MILLION_TOKENS = float(os.environ.get("API_COST_PER_MILLION_TOKENS", "0.25"))

# Planes comerciales (fuente única de verdad para backend y UI)
PLAN_FREE_MINUTES = float(os.environ.get("PLAN_FREE_MINUTES", "10.0"))
PLAN_FREE_TOKENS = int(os.environ.get("PLAN_FREE_TOKENS", str(int(10 * 72200))))
PLAN_PRO_MINUTES = int(os.environ.get("PLAN_PRO_MINUTES", "180"))
PLAN_PRO_TOKENS = int(os.environ.get("PLAN_PRO_TOKENS", str(int(180 * 72200))))
PLAN_PRO_PRICE_USD = float(os.environ.get("PLAN_PRO_PRICE_USD", "29"))
PLAN_CUSTOM_MINUTES = int(os.environ.get("PLAN_CUSTOM_MINUTES", "600"))
PLAN_CUSTOM_TOKENS = int(os.environ.get("PLAN_CUSTOM_TOKENS", str(int(600 * 72200))))
PLAN_CUSTOM_PRICE_USD = float(os.environ.get("PLAN_CUSTOM_PRICE_USD", "99"))
PLAN_OVERAGE_PRICE_PER_MIN = float(os.environ.get("PLAN_OVERAGE_PRICE_PER_MIN", "0.19"))

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from openai import OpenAI
import httpx
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip, AudioClip
import numpy as np
from celery import Celery

# Autoinstalación de dependencias críticas
def install_dep(package):
    try:
        __import__(package.replace("-", "_"))
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

install_dep("edge-tts")
install_dep("flask-cors")
install_dep("openai")
install_dep("httpx")
install_dep("soundfile")
install_dep("silero-vad")
install_dep("celery")
install_dep("redis")
install_dep("Pillow")
from PIL import Image

import edge_tts
from silero_vad import load_silero_vad, get_speech_timestamps, read_audio

# Inicialización de Redis para comunicación entre contenedores
import redis
broker_url = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
redis_client = redis.Redis.from_url(broker_url)

def recargar_claves_desde_redis():
    """Carga claves de API globales desde Redis si están disponibles."""
    global OPENAI_API_KEY, OPENROUTER_API_KEY, OPENROUTER_FREE_MODEL
    try:
        ck_openai = redis_client.get("config:openai_api_key")
        if ck_openai:
            val_clean = ck_openai.decode('utf-8').strip()
            if "test-key" in val_clean:
                try: redis_client.delete("config:openai_api_key")
                except: pass
                OPENAI_API_KEY = ""
            else:
                OPENAI_API_KEY = val_clean
        ck_or = redis_client.get("config:openrouter_api_key")
        if ck_or:
            OPENROUTER_API_KEY = ck_or.decode('utf-8').strip()
        ck_model = redis_client.get("config:openrouter_free_model")
        if ck_model:
            OPENROUTER_FREE_MODEL = ck_model.decode('utf-8').strip()
    except Exception as e:
        print(f"[Redis Config Warning] No se pudieron leer claves globales de Redis: {e}")

# Intento inicial al cargar módulo
try:
    recargar_claves_desde_redis()
except Exception:
    pass

# ==============================================================================
# POOL DE CLAVES GRATUITAS (ROTACIÓN Y AUTO-FAILOVER)
# ==============================================================================
def obtener_pool_claves_gratuitas():
    """Recupera el pool de claves gratuitas desde Redis o inicializa con clave por defecto."""
    try:
        raw = redis_client.get("config:free_keys_pool")
        if raw:
            keys = json.loads(raw.decode('utf-8'))
            if isinstance(keys, list) and len(keys) > 0:
                return keys
    except Exception as e:
        print(f"[Free Keys Pool Warning] No se pudo leer pool de Redis: {e}")

    # Fallback si el pool está vacío pero existe OPENROUTER_API_KEY
    if OPENROUTER_API_KEY:
        default_key = {
            "id": "gemini-default",
            "name": "Google AI Studio Principal (Env)",
            "api_key": OPENROUTER_API_KEY,
            "base_url": OPENROUTER_BASE_URL,
            "model": OPENROUTER_FREE_MODEL,
            "status": "active",
            "uses_count": 0,
            "last_error": None,
            "created_at": int(time.time())
        }
        guardar_pool_claves_gratuitas([default_key])
        return [default_key]
    return []

def guardar_pool_claves_gratuitas(pool):
    """Persiste el pool de claves gratuitas en Redis."""
    try:
        redis_client.set("config:free_keys_pool", json.dumps(pool, ensure_ascii=False), ex=86400 * 365)
    except Exception as e:
        print(f"[Free Keys Pool Error] No se pudo persistir pool en Redis: {e}")

def resolver_config_clave(api_key, name="", model=None, base_url=None):
    """
    Resuelve la configuración de conexión IA respetando explícitamente el modelo y base_url configurados.
    Elimina deducciones automáticas por prefijo para modelos obsoletos.
    """
    raw_key = (api_key or "").strip()
    explicit_model = (model or "").strip()
    explicit_url = (base_url or "").strip()

    # Si se especificó explícitamente modelo gemini-3.5-flash-lite (o variante gemini), fijar estrictamente su endpoint
    if explicit_model == "gemini-3.5-flash-lite" or "gemini" in explicit_model.lower():
        return "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash-lite"

    # Si viene con modelo y base_url específicos y no es gemini, respetarlos directamente
    if explicit_model and explicit_url:
        return explicit_url, explicit_model

    # Conserva únicamente la detección de OpenAI directa (sk-... que no sea sk-or-) para Premium / Superadmin
    if raw_key.startswith("sk-") and not raw_key.startswith("sk-or-"):
        return "https://api.openai.com/v1", "gpt-4o-mini"

    # Fallback estricto por defecto: Google AI Studio gemini-3.5-flash-lite
    return OPENROUTER_BASE_URL, OPENROUTER_FREE_MODEL


def crear_cliente_ia_para_clave(key_obj):
    """Crea una instancia de cliente OpenAI configurada para una clave específica del pool."""
    raw_key = (key_obj.get("api_key") or "").strip()
    name = (key_obj.get("name") or "").strip()
    k_model = (key_obj.get("model") or "").strip()
    k_url = (key_obj.get("base_url") or "").strip()

    base_url, model_name = resolver_config_clave(raw_key, name, model=k_model, base_url=k_url)

    headers = {}
    if "openrouter" in base_url or raw_key.startswith("sk-or-"):
        headers = {
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE
        }

    http_client = httpx.Client(trust_env=False, timeout=60.0)
    client = OpenAI(
        base_url=base_url,
        api_key=raw_key,
        default_headers=headers if headers else None,
        http_client=http_client
    )
    return client, model_name

# Auxiliares de lectura y guardado de status en Redis
def guardar_fase1_status(proyecto_id, status, error_message=None):
    try:
        data = {"status": status, "error": error_message}
        redis_client.set(f"fase1_status:{proyecto_id}", json.dumps(data), ex=86400)  # Expira en 24h
    except Exception as e:
        print(f"[Redis Error] No se pudo escribir status para {proyecto_id}: {e}")

def obtener_fase1_status(proyecto_id):
    try:
        val = redis_client.get(f"fase1_status:{proyecto_id}")
        if val:
            data = json.loads(val.decode('utf-8'))
            return data.get("status", "idle"), data.get("error")
    except Exception as e:
        print(f"[Redis Error] No se pudo leer status para {proyecto_id}: {e}")
    return "idle", None

# ==============================================================================
# SEGURIDAD Y RESTRICCIÓN ESTRICTA DE SUPERADMINISTRADOR
# ==============================================================================
def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == "OPTIONS":
            return f(*args, **kwargs)
        caller_email = (
            request.headers.get("X-User-Email") or 
            request.headers.get("X-Admin-Email") or
            request.args.get("admin_email") or
            (request.is_json and (request.json or {}).get("admin_email"))
        )
        if not caller_email or str(caller_email).strip().lower() != ADMIN_EMAIL:
            return jsonify({
                "status": "error",
                "error": "Acceso denegado (403 Forbidden)",
                "message": f"Se requieren privilegios del Superadministrador único ({ADMIN_EMAIL})."
            }), 403
        return f(*args, **kwargs)
    return decorated_function

def assert_no_unauthorized_admin(target_email, requested_role):
    """Restricción estricta: Bloquea cualquier intento de asignar rol 'admin' a otro usuario."""
    if requested_role == "admin" and (not target_email or str(target_email).strip().lower() != ADMIN_EMAIL):
        raise PermissionError(f"Prohibido: Solo {ADMIN_EMAIL} puede poseer el rol de Superadministrador.")

# ==============================================================================
# ALGORITMO DE CÁLCULO DE RENTABILIDAD (MINUTOS VS TOKENS)
# ==============================================================================
def calcular_rentabilidad(minutos, costo_por_millon=API_COST_PER_MILLION_TOKENS, vision_por_min=DEFAULT_VISION_TOKENS_PER_MIN, texto_por_min=DEFAULT_TEXT_TOKENS_PER_MIN, margen_objetivo=TARGET_GROSS_MARGIN_PERCENT):
    """
    Calcula tokens, costo directo de API y precio mínimo para alcanzar el
    margen bruto objetivo. El margen se calcula sobre el precio de venta:
        margen = (precio - costo) / precio
    Un margen del 50% equivale a que el costo de API sea como máximo el 50%
    del precio cobrado (precio = costo / 0.50 = costo x 2).
    """
    tokens_por_min = vision_por_min + texto_por_min
    tokens_totales = int(minutos * tokens_por_min)
    costo_api = (tokens_totales / 1000000.0) * costo_por_millon

    factor_costo_max = (100.0 - margen_objetivo) / 100.0
    precio_min_sugerido = (costo_api / factor_costo_max) if factor_costo_max > 0 else costo_api
    precio_max_sugerido = precio_min_sugerido * 1.50
    margen_calculado = ((precio_min_sugerido - costo_api) / precio_min_sugerido) * 100.0 if precio_min_sugerido > 0 else 0
    ganancia_bruta = precio_min_sugerido - costo_api

    return {
        "minutos": minutos,
        "tokens_por_min": tokens_por_min,
        "tokens_totales": tokens_totales,
        "costo_api_usd": round(costo_api, 2),
        "costo_por_minuto_usd": round(costo_api / minutos, 4) if minutos else 0,
        "precio_min_sugerido_usd": round(precio_min_sugerido, 2),
        "precio_max_sugerido_usd": round(precio_max_sugerido, 2),
        "precio_por_minuto_usd": round(precio_min_sugerido / minutos, 4) if minutos else 0,
        "ganancia_bruta_usd": round(ganancia_bruta, 2),
        "margen_bruto_porcentaje": round(margen_calculado, 1),
        "markup_sobre_costo_porcentaje": round((ganancia_bruta / costo_api) * 100.0, 1) if costo_api > 0 else 0
    }

# Reglas profesionales de RAG
REGLAS_PROFESIONALES = """
1. IDIOMA: Escribe exclusivamente en español de España (castellano peninsular). Evita palabras en inglés o anglicismos.
2. PERSONAJES: NUNCA digas 'hombre' o 'mujer' a secas. Di 'el hombre' o 'la mujer' y dales una caracteristica fisica.
3. PRECISIÓN DE GÉNERO (CRÍTICO): Observa DETENIDAMENTE antes de decidir. ¡CUIDADO! Hay un hombre con CABELLO LARGO. NO asumas que el cabello largo significa "mujer". Fíjate en la estructura física, el rostro y la barra para no confundirlos. Si están en mala posición o lejos, usa 'una persona'.
4. OBJETIVIDAD: Describe acciones, no sentimientos. Usa presente simple.
5. FLUJO: Describe de izquierda a derecha si hay varios elementos.
6. BREVE Y FLUIDO: Usa presente simple y ve directo a la acción principal.
7. IDENTIDAD FÍSICA: NUNCA uses la palabra 'figura'. ALTERNA: nombra el rasgo físico completo 1 de cada 3 veces.
8. NO REPETIR FRASES: Prohibido repetir estructuras del historial reciente.
9. PROHIBIDO: No menciones que alguien habla o gesticula (Describe la posición de manos o rostro).
10. CAMBIOS DE ESCENA: MIRA EL HISTORIAL. Si sigues en el mismo lugar que en el historial anterior, ESTÁ ESTRICTAMENTE PROHIBIDO repetir el lugar o momento del día; describe DIRECTO la acción. SOLO si el entorno actual es CLARAMENTE NUEVO y DIFERENTE, empieza tu frase indicando el LUGAR.
11. OMISIÓN DE SUJETO REPETIDO (IMPORTANTE): Si el personaje de tu oración es exactamente el mismo que actuó en la línea inmediatamente anterior del HISTORIAL, ESTÁ PROHIBIDO volver a nombrarlo ("El hombre...", "La mujer..."). Omite el sujeto e inicia tu frase DIRECTO CON LA ACCIÓN (ej: "Se inclina", "Camina hacia la puerta"). PERO si apareció otro personaje entre medias, debes volver a presentarlo con la descripción física habitual.
"""

EJEMPLOS_ESTILO = """
Continuación: 'El hombre de anteojos asiente en silencio.'
Cambio de Escena: 'NOCHE, CALLE. La mujer de abrigo gris camina apresurada.'
Inicio: 'DÍA, OFICINA. El hombre teclea en su computadora.'
Sujeto Repetido (Elipsis, asumiendo que el historial es del mismo hombre): 'Se inclina sobre la mesa and cruza los brazos.'
Mal: 'Un tipo esta triste.'
"""

app = Flask(__name__)
CORS(app)

# Configuración de Celery
broker_url = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
celery_app = Celery(
    'tu_script',
    broker=broker_url,
    backend=broker_url
)
celery_app.conf.update(
    task_track_started=True,
    task_time_limit=1800,  # 30 minutos de límite
)

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# Función para descargar un archivo de internet con User-Agent y soporte de streaming
def descargar_video(url, dest_path):
    print(f"[Info] Descargando video desde {url} hacia {dest_path}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as f_out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f_out.write(chunk)
    print(f"[Info] Descarga completada: {os.path.getsize(dest_path)} bytes.")

# --- EXTRACCIÓN DE FOTOGRAMA ROBUSTA Y ADAPTATIVA (SOPORTE VERTICAL / HORIZONTAL) ---
def extraer_frame_robusto(video_path, t, output_path, max_dim=1024):
    """
    Extrae un fotograma optimizado en el segundo t con rotación corregida (-autorotate)
    y tamaño adaptativo (máximo max_dim px), asegurando modo RGB sin fallos en videos verticales u horizontales.
    """
    # 1. Extracción directa con FFmpeg (rápida, corrige orientación de celulares automáticamente)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{max(0.0, float(t)):.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale='min({max_dim},iw)':-2",
            "-q:v", "3",
            output_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 500:
            return True
    except Exception as e_ff:
        print(f"[Frame FFmpeg Warning] {e_ff}")

    # 2. Respaldo MoviePy + PIL (resuelve canales RGBA a RGB y redimensiona)
    try:
        with VideoFileClip(video_path) as clip:
            dur = float(clip.duration or 1.0)
            safe_t = max(0.0, min(float(t), dur - 0.05))
            raw_frame = clip.get_frame(safe_t)
            im = Image.fromarray(raw_frame)
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            im.save(output_path, "JPEG", quality=82, optimize=True)
            return True
    except Exception as e_mv:
        print(f"[Frame MoviePy Warning] {e_mv}")

    # 3. Fallback dummy si el video no tiene fotograma legible en esa posición
    try:
        img_dummy = Image.new("RGB", (640, 360), color=(25, 25, 25))
        img_dummy.save(output_path, "JPEG", quality=80)
        return True
    except Exception:
        return False

# --- 1. DETECCIÓN DE HUECOS (VAD) CON TOLERANCIA TOTAL A VIDEOS SIN AUDIO / VERTICALES ---
def obtener_huecos_silencio(video_path, min_gap=2.2, max_intervalo=6.0):
    print(f"[Info] Analizando silencios y estructura de: {os.path.basename(video_path)}")
    duracion_total = 0.0
    try:
        with VideoFileClip(video_path) as v_clip:
            duracion_total = float(v_clip.duration or 0.0)
    except Exception as e_dur:
        print(f"[Warning] No se pudo leer duración con MoviePy: {e_dur}")
        try:
            cmd_probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            res_p = subprocess.run(cmd_probe, capture_output=True, text=True, timeout=5)
            if res_p.returncode == 0 and res_p.stdout.strip():
                duracion_total = float(res_p.stdout.strip())
        except Exception:
            pass

    if duracion_total <= 0:
        duracion_total = 5.0

    audio_temp = os.path.join(TEMP_DIR, f"temp_vad_{uuid.uuid4().hex}.wav")
    audio_extraido = False

    # 1. Intentar extracción directa con FFmpeg (más tolerante a códecs de celulares que MoviePy)
    try:
        cmd_ff = ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_temp]
        res_ff = subprocess.run(cmd_ff, capture_output=True, text=True, timeout=30)
        if res_ff.returncode == 0 and os.path.exists(audio_temp) and os.path.getsize(audio_temp) > 1000:
            audio_extraido = True
    except Exception as e_ff:
        print(f"[VAD FFmpeg Warning]: {e_ff}")

    # 2. Respaldo con MoviePy si FFmpeg no completó
    if not audio_extraido:
        try:
            with VideoFileClip(video_path) as video:
                if video.audio is not None:
                    video.audio.write_audiofile(audio_temp, fps=16000, nbytes=2, codec='pcm_s16le', verbose=False, logger=None)
                    if os.path.exists(audio_temp) and os.path.getsize(audio_temp) > 1000:
                        audio_extraido = True
        except Exception as e_mv:
            print(f"[VAD MoviePy Warning]: {e_mv}")

    silencios = []

    # 3. Si hay pista de audio, aplicar Silero VAD
    if audio_extraido:
        try:
            vad_model = load_silero_vad()
            wav = read_audio(audio_temp, sampling_rate=16000)
            speech_ts = get_speech_timestamps(wav, vad_model, sampling_rate=16000)
            
            cursor = 0.0
            for ts in speech_ts:
                inicio_voz = ts['start'] / 16000
                if inicio_voz - cursor >= min_gap:
                    silencios.append((cursor, inicio_voz))
                cursor = ts['end'] / 16000
                
            if duracion_total - cursor >= min_gap:
                silencios.append((cursor, duracion_total))
        except Exception as e_vad:
            print(f"[Silero VAD Error]: {e_vad}")
        finally:
            try:
                if os.path.exists(audio_temp): os.remove(audio_temp)
            except:
                pass

    # 4. Si el video es mudo, o no tiene audio, o no hubo huecos >= min_gap:
    # Generar intervalos automáticos basados en la duración para garantizar audiodescripción completa
    if not silencios:
        print("[VAD Info] Video sin pista de voz detectable o mudo. Generando intervalos visuales automáticos.")
        paso = min(max_intervalo, max(3.0, duracion_total / max(1, math.ceil(duracion_total / 5.0))))
        curr = 0.0
        while curr < duracion_total:
            sig = min(curr + paso, duracion_total)
            silencios.append((curr, sig))
            curr = sig

    huecos_subdivididos = []
    for inicio, fin in silencios:
        duracion = fin - inicio
        if duracion > max_intervalo * 1.3:
            cursor_trozo = inicio
            while fin - cursor_trozo > max_intervalo * 1.3:
                huecos_subdivididos.append((round(cursor_trozo, 2), round(cursor_trozo + max_intervalo, 2)))
                cursor_trozo += max_intervalo
            huecos_subdivididos.append((round(cursor_trozo, 2), round(fin, 2)))
        else:
            huecos_subdivididos.append((round(inicio, 2), round(fin, 2)))

    if not huecos_subdivididos:
        huecos_subdivididos.append((0.0, round(duracion_total, 2)))

    return huecos_subdivididos

# --- 2. ANÁLISIS CON OPENAI / OPENROUTER ---
def analizar_con_openai(openai_client, video_path, inicio, fin, indice_escena, historial, model_name="gpt-4o-mini", ocr_enabled=True, modo="audiodescripcion"):
    duracion_max = fin - inicio
    print(f"   [Info] Analizando escena {indice_escena} ({model_name}, modo={modo}): {inicio:.1f}s a {fin:.1f}s")
    
    frame_path = os.path.join(TEMP_DIR, f"frame_desc_{uuid.uuid4().hex}.jpg")
    t_frame = max(0.05, inicio + 0.5)
    
    # Extraer fotograma robusto adaptativo (máx 1024px, autorotate, RGB asegurado)
    frame_ok = extraer_frame_robusto(video_path, t_frame, frame_path, max_dim=1024)
    base64_image = encode_image(frame_path) if (frame_ok and os.path.exists(frame_path)) else None

    palabras = max(15, int(duracion_max * 2.2))
    
    if modo == "texto_a_voz":
        prompt_sistema = """Eres un sistema especializado de transcripción y lectura de texto en pantalla a voz (OCR visual de video a voz) para personas con discapacidad visual.
Tu ÚNICA labor es encontrar cualquier texto visible en la imagen (carteles, letreros, mensajes de texto, títulos, subtítulos, créditos, rótulos o avisos) y transcribirlo con precisión para ser locutado.
REGLAS ESTRICTAS PARA MODO TEXTO A VOZ:
1. Si encuentras texto legible en la imagen, transcríbelo directamente para locución (ej: 'En pantalla se lee: [texto]' o simplemente el contenido del cartel/texto).
2. Si NO hay ningún texto visible o legible en la imagen, responde ÚNICAMENTE la palabra: 'SILENCIO'.
3. NO describas acciones de personajes, ni ropa, ni iluminación, ni paisajes. Tu función exclusiva es convertir el TEXTO de la imagen a VOZ."""
        prompt_usuario = f"""Examina minuciosamente la imagen adjunta. ¿Hay algún texto, rótulo, título, subtítulo o cartel visible?
Si hay texto, léelo/transcríbelo fielmente para que sea narrado. Si NO hay texto visible, responde exclusivamente: SILENCIO"""
    else:
        instruccion_escena = (
            "OBLIGATORIO: Al ser la primera escena del video, DEBES empezar tu descripción indicando el LUGAR (y DÍA/NOCHE solo si es inconfundible), seguido de la acción (ej: 'CALLE. El hombre...', o 'NOCHE, EXTERIOR.')." 
            if indice_escena == 0 else 
            "PROHIBIDO NOMBRAR EL LUGAR O CLIMA si estás en el mismo entorno del HISTORIAL. Ve directo a la acción. SOLO si el LUGAR cambia drásticamente respecto al historial, menciónalo al principio."
        )
        historial_str = " | ".join(historial[-3:]) if historial else "Ninguno"
        
        prompt_sistema = f"""Eres un audiodescriptor profesional experto en sintesis.
MANUAL DE REGLAS:
{REGLAS_PROFESIONALES}
CONTEXTO DE ESCENA: {instruccion_escena}"""
        prompt_usuario = f"""EJEMPLOS:
{EJEMPLOS_ESTILO}
HISTORIAL RECIENTE: {historial_str}
Genera UNA SOLA oracion corta (max {palabras} palabras) sobre la imagen. Aplica las reglas. IMPORTANTE: ¡NO repitas el lugar ni la hora si el historial dice que ya estamos ahí! Termina con punto."""
    p_sistema_utf8 = prompt_sistema.encode('utf-8').decode('utf-8')
    p_usuario_utf8 = prompt_usuario.encode('utf-8').decode('utf-8')

    max_retries = 5
    base_delay = 2.0
    resultado = "Acción en pantalla."
    tokens_spent = 0
    
    user_content = [
        {"type": "text", "text": p_usuario_utf8}
    ]
    if base64_image:
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}})

    for attempt in range(max_retries):
        try:
            response = openai_client.chat.completions.create(
                model=model_name,
                max_tokens=75,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": p_sistema_utf8},
                    {"role": "user", "content": user_content}
                ]
            )
            resultado = response.choices[0].message.content.strip()
            if hasattr(response, 'usage') and response.usage:
                tokens_spent = getattr(response.usage, 'total_tokens', 0)
            if not tokens_spent:
                tokens_spent = len(p_sistema_utf8.split()) + len(p_usuario_utf8.split()) + len(resultado.split()) + 600
            break
        except Exception as e:
            error_str = str(e).lower()
            transient_signals = ['rate_limit', '429', '503', 'unavailable', 'high demand', 'too many requests', 'resource_exhausted', 'overloaded']
            if any(sig in error_str for sig in transient_signals):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   [Wait] Error temporal ({error_str[:60]}). Backoff {delay:.1f} seg... (Intento {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                else:
                    print(f"   [Failover Trigger] Error 429/503 persistente tras {max_retries} intentos en clave de {model_name}.")
                    raise RuntimeError(f"API RateLimit/Quota agotada (429/503 persistente en {model_name}): {e}")

            print(f"   [Error] Error IA ({model_name}): {e}")
            if any(k in error_str for k in ["authentication", "invalid_api_key", "401", "incorrect api key"]):
                raise RuntimeError(f"Clave de API inválida o no autorizada para {model_name} (401). Ingresa tu clave real en el Panel Admin.")
            if any(k in error_str for k in ["insufficient_quota", "quota", "billing"]):
                raise RuntimeError(f"Saldo insuficiente o cuota agotada en tu cuenta de OpenAI/OpenRouter (429). Revisa tu saldo.")
            # Fallback si el modelo falla por formato de imagen o payload
            if base64_image and any(k in error_str for k in ["vision", "image", "unsupported", "payload", "400", "413", "422"]):
                try:
                    print("   [Fallback] Reintentando con texto como fallback...")
                    fb_response = openai_client.chat.completions.create(
                        model=model_name,
                        max_tokens=50,
                        messages=[
                            {"role": "system", "content": p_sistema_utf8},
                            {"role": "user", "content": p_usuario_utf8}
                        ]
                    )
                    resultado = fb_response.choices[0].message.content.strip()
                    tokens_spent = 200
                    break
                except Exception as fb_err:
                    print(f"   [Fallback Error]: {fb_err}")
            break

    try:
        if os.path.exists(frame_path):
            os.remove(frame_path)
    except:
        pass

    if modo == "texto_a_voz":
        clean_res = resultado.strip().strip('"').strip("'").upper()
        if clean_res in ["SILENCIO", "SILENCIO.", "NINGUNO", "NINGUNO.", "NO HAY TEXTO", "NO HAY TEXTO.", "NO HAY TEXTO VISIBLE."]:
            print(f"   [Fase 1 OCR] Escena {indice_escena}: Sin texto en pantalla. Silencio. (Tokens: {tokens_spent})")
            return "", tokens_spent

    return resultado, tokens_spent

# --- UTILERÍA DE CACHÉ DE AUDIO TTS ---
def obtener_ruta_cache_tts(texto, voz, rate="+0%"):
    import hashlib
    filename = f"tts_{hashlib.md5((texto + voz + rate).encode('utf-8')).hexdigest()}.mp3"
    return os.path.join(CARPETA_AUDIOS, filename)

# --- OPTIMIZACIÓN DE AUDIO PARA COMPATIBILIDAD NATIVA (FIREFOX) ---
def optimizar_mp3_firefox(path_audio):
    temp_ffmpeg = path_audio + ".temp.mp3"
    try:
        cmd_ffmpeg = ["ffmpeg", "-y", "-i", path_audio, "-codec:a", "libmp3lame", "-ar", "44100", temp_ffmpeg]
        res_ffmpeg = subprocess.run(cmd_ffmpeg, capture_output=True, text=True, timeout=8)
        if res_ffmpeg.returncode == 0 and os.path.exists(temp_ffmpeg) and os.path.getsize(temp_ffmpeg) > 0:
            os.replace(temp_ffmpeg, path_audio)
    except Exception as e_ffmpeg:
        print(f"[FFmpeg Warning] No se pudo ejecutar optimización de audio: {e_ffmpeg}")
    finally:
        if os.path.exists(temp_ffmpeg):
            try: os.remove(temp_ffmpeg)
            except: pass

# --- GENERACIÓN DE AUDIO (EDGE TTS) ---
def generar_tts_robusto(texto, voz, path_salida, rate="+0%"):
    if rate:
        rate = str(rate).strip()
    if not rate or rate in ["0%", "0", " 0%"]:
        rate = "+0%"
    elif not (rate.startswith("+") or rate.startswith("-")):
        rate = f"+{rate}"

    if os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
        optimizar_mp3_firefox(path_salida)
        return True, ""

    os.makedirs(os.path.dirname(path_salida), exist_ok=True)

    async def amain():
        communicate = edge_tts.Communicate(texto, voz, rate=rate)
        await communicate.save(path_salida)

    errores = []

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(amain())
        loop.close()
        if os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
            optimizar_mp3_firefox(path_salida)
            return True, ""
    except Exception as e:
        err_msg = f"new_event_loop: {type(e).__name__}: {str(e)}"
        errores.append(err_msg)

    try:
        asyncio.run(amain())
        if os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
            optimizar_mp3_firefox(path_salida)
            return True, ""
    except Exception as e:
        err_msg = f"asyncio.run: {type(e).__name__}: {str(e)}"
        errores.append(err_msg)

    try:
        cli_rate_arg = f"--rate={rate}" if rate and rate != "+0%" else ""
        cmd = [sys.executable, "-m", "edge_tts", "--text", texto, "--voice", voz, "--write-media", path_salida]
        if cli_rate_arg:
            cmd.append(cli_rate_arg)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
            optimizar_mp3_firefox(path_salida)
            return True, ""
        else:
            err_msg = f"CLI (returncode={res.returncode}): stdout={res.stdout.strip()}, stderr={res.stderr.strip()}"
            errores.append(err_msg)
    except Exception as e:
        err_msg = f"CLI_exception: {type(e).__name__}: {str(e)}"
        errores.append(err_msg)

    return False, " | ".join(errores)

def generar_audio_pro(texto, i, voz_id, proyecto_id=None, rate="+0%"):
    proj_prefix = f"{proyecto_id}_" if proyecto_id else ""
    path_audio = os.path.join(TEMP_DIR, f"ad_{proj_prefix}{i}_{uuid.uuid4().hex}.mp3")
    voice = voz_id if voz_id else "es-ES-AlvaroNeural" 
    
    ruta_cache = obtener_ruta_cache_tts(texto, voice, rate)
    if os.path.exists(ruta_cache) and os.path.getsize(ruta_cache) > 0:
        try:
            import shutil
            shutil.copyfile(ruta_cache, path_audio)
            return path_audio
        except Exception as e:
            print(f"[Warning] No se pudo copiar desde caché: {e}")

    exito, _ = generar_tts_robusto(texto, voice, path_audio, rate)
    if exito:
        try:
            import shutil
            shutil.copyfile(path_audio, ruta_cache)
        except:
            pass
        return path_audio
    else:
        print(f"[Error] Error en TTS Edge al generar audio pro.")
        return None

# ==============================================================================
# TAREA CELERY: PROCESAMIENTO CON CONTROL DE CONSUMO Y MODELOS OPENROUTER/OPENAI
# ==============================================================================
@celery_app.task(name='tu_script.procesar_video_task')
def procesar_video_task(video_url, api_key, proyecto_id, *args, **kwargs):
    user_email = kwargs.get("user_email") or (args[0] if len(args) > 0 else None)
    uid = kwargs.get("uid") or (args[1] if len(args) > 1 else None)
    role = kwargs.get("role") or (args[2] if len(args) > 2 else "free")
    user_tokens_used = kwargs.get("user_tokens_used") or (args[3] if len(args) > 3 else 0)
    user_tokens_total = kwargs.get("user_tokens_total") or (args[4] if len(args) > 4 else PLAN_FREE_TOKENS)
    selected_voice = kwargs.get("voice") or "es-ES-AlvaroNeural"
    modo = kwargs.get("modo") or "audiodescripcion"
    ocr_enabled = kwargs.get("ocr_enabled") if "ocr_enabled" in kwargs else (modo == "texto_a_voz")
    print(f"[Celery] Iniciando Fase 1: proyecto={proyecto_id}, user={user_email}, role={role}")
    guardar_fase1_status(proyecto_id, "processing")
    video_path = os.path.join(TEMP_DIR, f"{proyecto_id}_original.mp4")

    try:
        # Descargar video
        descargar_video(video_url, video_path)

        # 1. Resolver rol estricto
        if user_email and user_email.strip().lower() == ADMIN_EMAIL:
            role = "admin"
        elif role == "admin":
            role = "free"

        # 2. Control de Duración y Minutos Consumidos
        with VideoFileClip(video_path) as v_clip:
            duracion_segundos = v_clip.duration
        duracion_minutos = round(duracion_segundos / 60.0, 2)
        tokens_estimados = int(duracion_minutos * (DEFAULT_VISION_TOKENS_PER_MIN + DEFAULT_TEXT_TOKENS_PER_MIN))

        # Recuperar estado actualizado de minutos y tokens desde Redis
        cur_tokens_used = max(0, int(user_tokens_used or 0))
        cur_tokens_total = int(user_tokens_total or 0)
        cur_minutes_used = 0.0
        cur_minutes_total = 10.0 if role == "free" else (180.0 if role == "premium" else 999999.0)
        assigned_free_key_name = ""
        assigned_free_key_id = ""
        last_used_key_name = "Ninguna"

        if uid:
            try:
                raw_u = redis_client.get(f"user_data:{uid}")
                if raw_u:
                    u_cached = json.loads(raw_u.decode('utf-8'))
                    cur_tokens_used = int(u_cached.get("tokens_used", cur_tokens_used) or 0)
                    cur_tokens_total = int(u_cached.get("tokens_total", cur_tokens_total) or 0)
                    cur_minutes_used = float(u_cached.get("minutes_used", 0.0) or 0.0)
                    cur_minutes_total = float(u_cached.get("minutes_total", cur_minutes_total) or cur_minutes_total)
                    assigned_free_key_name = (u_cached.get("assigned_free_key_name") or "").strip()
                    assigned_free_key_id = (u_cached.get("assigned_free_key_id") or "").strip()
                    last_used_key_name = u_cached.get("last_used_key_name", "Ninguna")
            except Exception as e_red:
                print(f"[Redis Cache Warning]: {e_red}")

        # Bloqueo estricto por MINUTOS DE VIDEO (El límite no se resetea al borrar proyectos)
        if role == "free" and (cur_minutes_used + duracion_minutos) > cur_minutes_total:
            err_msg = (
                f"Límite de minutos de video agotado. Has utilizado {cur_minutes_used:.1f} de {cur_minutes_total:.1f} minutos "
                f"permitidos en tu cuenta gratuita. Este video dura {duracion_minutos:.1f} min. "
                f"Para continuar audiodescribiendo más videos, adquiere un plan Pro."
            )
            print(f"[Bloqueo Minutos] {err_msg}")
            guardar_fase1_status(proyecto_id, "quota_exceeded", err_msg)
            return {"status": "quota_exceeded", "message": err_msg}

        # El plan Pro necesita una API key asignada por el administrador
        if role == "premium" and not api_key:
            err_msg = "Tu plan Pro aún no tiene una API key asignada por el administrador."
            guardar_fase1_status(proyecto_id, "error", err_msg)
            return {"status": "error", "message": err_msg}

        # Recargar claves globales dinámicamente
        recargar_claves_desde_redis()
        http_client = httpx.Client(trust_env=False, timeout=60.0)

        # 3. Preparación del Cliente de IA (Pool con Rotación y Failover para Free, Key dedicada para Pro)
        free_keys_pool = []
        active_free_keys = []
        current_free_key_idx = 0

        if role == "free":
            free_keys_pool = obtener_pool_claves_gratuitas()
            # Ordenar claves: si el usuario tiene una clave asignada específica, ponerla de primera si está activa
            active_free_keys = [k for k in free_keys_pool if k.get("status") == "active"]
            if assigned_free_key_name or assigned_free_key_id:
                assigned_matches = [
                    k for k in active_free_keys 
                    if k.get("name") == assigned_free_key_name or k.get("id") == assigned_free_key_id
                ]
                if assigned_matches:
                    active_free_keys = assigned_matches + [k for k in active_free_keys if k not in assigned_matches]

            if not active_free_keys and not OPENAI_API_KEY:
                err_msg = (
                    "Todas las API keys gratuitas del servidor están suspendidas o agotaron su cuota. "
                    "El administrador debe agregar o reactivar claves en el Panel Admin."
                )
                guardar_fase1_status(proyecto_id, "error", err_msg)
                return {"status": "error", "message": err_msg}

        # 4. Detección de silencios Silero VAD
        huecos = obtener_huecos_silencio(video_path)
        historial_ad = []
        bloques_guion = []
        tokens_reales_acumulados = 0

        for i, (inicio, fin) in enumerate(huecos):
            texto = ""
            tokens_escena = 0

            if role == "free":
                # Intentar procesar la escena usando el pool de claves con failover automático
                scene_success = False
                while not scene_success and current_free_key_idx < len(active_free_keys):
                    curr_key = active_free_keys[current_free_key_idx]
                    try:
                        ai_client, model_name = crear_cliente_ia_para_clave(curr_key)
                        texto, tokens_escena = analizar_con_openai(
                            ai_client, video_path, inicio, fin, i, historial_ad, model_name=model_name
                        )
                        scene_success = True
                        last_used_key_name = curr_key.get("name", "Clave Gratuita")
                        curr_key["uses_count"] = curr_key.get("uses_count", 0) + 1
                    except Exception as e_ia:
                        err_str = str(e_ia)
                        print(f"[Failover Warning] Escena {i}: Clave '{curr_key.get('name')}' falló: {err_str}")
                        # Marcar clave como suspendida con el motivo del fallo
                        curr_key["status"] = "suspended"
                        curr_key["last_error"] = f"{err_str[:150]} ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})"
                        # Sincronizar en el pool general y guardar en Redis
                        for pk in free_keys_pool:
                            if pk.get("id") == curr_key.get("id"):
                                pk["status"] = "suspended"
                                pk["last_error"] = curr_key["last_error"]
                        guardar_pool_claves_gratuitas(free_keys_pool)

                        # Avanzar a la siguiente clave activa
                        current_free_key_idx += 1
                        if current_free_key_idx < len(active_free_keys):
                            print(f"[Failover] Rotando a siguiente clave: '{active_free_keys[current_free_key_idx].get('name')}'")
                        else:
                            print("[Failover] Se agotaron las claves activas en el pool.")

                # Si fallaron todas las del pool pero existe OPENAI_API_KEY en servidor, usarla como salvaguarda
                if not scene_success:
                    if OPENAI_API_KEY:
                        print("[Failover Universal] Utilizando OpenAI Server Key como salvamento final.")
                        fb_client = OpenAI(api_key=OPENAI_API_KEY, http_client=http_client)
                        texto, tokens_escena = analizar_con_openai(
                            fb_client, video_path, inicio, fin, i, historial_ad, model_name="gpt-4o-mini"
                        )
                        last_used_key_name = "OpenAI Servidor (Salvamento)"
                        scene_success = True
                    else:
                        raise RuntimeError(
                            "Todas las API keys gratuitas del pool fallaron o agotaron su cuota (Error 429/401). "
                            "El administrador debe agregar o reactivar claves en el Panel Admin."
                        )
            else:
                # Rol Premium o Superadmin
                effective_key = api_key or OPENAI_API_KEY
                if not effective_key:
                    raise RuntimeError("No se encontró una API key válida configurada para procesar el video.")

                if effective_key.startswith("sk-or-"):
                    ai_client = OpenAI(
                        base_url=OPENROUTER_BASE_URL,
                        api_key=effective_key,
                        default_headers={"HTTP-Referer": OPENROUTER_REFERER, "X-Title": OPENROUTER_TITLE},
                        http_client=http_client
                    )
                    model_to_use = "openai/gpt-4o-mini"
                else:
                    ai_client = OpenAI(api_key=effective_key, http_client=http_client)
                    model_to_use = "gpt-4o-mini"

                texto, tokens_escena = analizar_con_openai(
                    ai_client, video_path, inicio, fin, i, historial_ad, model_name=model_to_use, ocr_enabled=ocr_enabled, modo=modo
                )
                last_used_key_name = "OpenAI Cuenta Pro" if role == "premium" else "OpenAI Superadmin"

            tokens_reales_acumulados += tokens_escena
            if modo == "texto_a_voz" and not texto:
                # En modo exclusivo Texto a Voz, las escenas sin texto no crean pista de audio
                continue
            historial_ad.append(texto)
            if len(historial_ad) > 6:
                historial_ad.pop(0)

            # Pre-generar TTS
            voz_default = selected_voice or "es-ES-AlvaroNeural"
            if texto:
                ruta_cache = obtener_ruta_cache_tts(texto, voz_default)
                generar_tts_robusto(texto, voz_default, ruta_cache)
                try:
                    clip_audio = AudioFileClip(ruta_cache)
                    duracion_audio = clip_audio.duration
                    clip_audio.close()
                except Exception:
                    duracion_audio = len(texto) / 15.0

                fin_limite = inicio + duracion_audio + 0.5
                if fin_limite < fin:
                    fin = float(f"{fin_limite:.2f}")

            bloques_guion.append({
                "id": i + 1,
                "start": inicio,
                "end": fin,
                "text": texto,
                "voice": voz_default,
                "audio_url": None
            })

            # Notificar progreso incremental en Redis para visualización en tiempo real en el editor
            progreso_stream = {
                "status": "processing",
                "current_scene": i + 1,
                "total_scenes": len(huecos),
                "percent": int(((i + 1) / max(1, len(huecos))) * 100),
                "bloques": list(bloques_guion),
                "last_added": bloques_guion[-1],
                "last_used_key_name": last_used_key_name
            }
            try:
                redis_client.set(f"fase1_progreso:{proyecto_id}", json.dumps(progreso_stream, ensure_ascii=False), ex=86400)
            except Exception:
                pass

        # 5. Acumular minutos y tokens de video (incluso si luego se borra el proyecto)
        nuevo_tokens_used = cur_tokens_used + tokens_reales_acumulados
        nuevo_minutes_used = round(cur_minutes_used + duracion_minutos, 2)

        if role == "free" and free_keys_pool:
            guardar_pool_claves_gratuitas(free_keys_pool)

        if uid:
            try:
                raw_u = redis_client.get(f"user_data:{uid}")
                u_cached = json.loads(raw_u.decode('utf-8')) if raw_u else {}
                u_cached.update({
                    "uid": uid,
                    "email": user_email or u_cached.get("email", ""),
                    "role": role,
                    "tokens_used": nuevo_tokens_used,
                    "tokens_total": cur_tokens_total,
                    "minutes_used": nuevo_minutes_used,
                    "minutes_total": cur_minutes_total,
                    "last_used_key_name": last_used_key_name,
                    "last_usage_at": int(time.time())
                })
                redis_client.set(f"user_data:{uid}", json.dumps(u_cached), ex=86400 * 30)
                print(f"[Minutos/Tokens] Consumo actualizado para {user_email} ({role}): {nuevo_minutes_used:.2f} min consumidos (+{duracion_minutos:.2f} min), key usada: {last_used_key_name}")
            except Exception as e_tok:
                print(f"[Redis Warning] {e_tok}")

        # Guardar resultado en Redis
        tokens_restantes = max(0, cur_tokens_total - nuevo_tokens_used)
        minutos_restantes = max(0.0, round(cur_minutes_total - nuevo_minutes_used, 2))

        res_payload = {
            "bloques": bloques_guion,
            "tokens_consumed": tokens_reales_acumulados,
            "tokens_used_total": nuevo_tokens_used,
            "tokens_total": cur_tokens_total,
            "tokens_remaining": tokens_restantes,
            "minutes_used": nuevo_minutes_used,
            "minutes_total": cur_minutes_total,
            "minutes_remaining": minutos_restantes,
            "duration_minutes": duracion_minutos,
            "last_used_key_name": last_used_key_name,
            "role": role
        }
        try:
            redis_client.set(f"fase1_resultado:{proyecto_id}", json.dumps(res_payload, ensure_ascii=False), ex=86400)
            # Guardar fase1_progreso completado
            progreso_final = {
                "status": "idle",
                "current_scene": len(bloques_guion),
                "total_scenes": len(bloques_guion),
                "percent": 100,
                "bloques": bloques_guion,
                "tokens_consumed": tokens_reales_acumulados,
                "minutes_used": nuevo_minutes_used,
                "last_used_key_name": last_used_key_name
            }
            redis_client.set(f"fase1_progreso:{proyecto_id}", json.dumps(progreso_final, ensure_ascii=False), ex=86400)
        except Exception as e_red:
            print(f"[Redis Error]: {e_red}")

        guardar_fase1_status(proyecto_id, "idle")
        print(f"[Celery] Tarea Fase 1 completada con éxito para {proyecto_id} ({len(bloques_guion)} bloques, key: {last_used_key_name})")
        return {
            "status": "success",
            "bloques_count": len(bloques_guion),
            "tokens_consumed": tokens_reales_acumulados,
            "minutes_used": nuevo_minutes_used,
            "last_used_key_name": last_used_key_name
        }

    except Exception as e:
        import traceback
        err_msg = str(e)
        traceback.print_exc()
        guardar_fase1_status(proyecto_id, "error", err_msg)
        print(f"[Celery Error] Falló Fase 1 para {proyecto_id}: {err_msg}")
        return {"status": "error", "message": err_msg}
# ==============================================================================
# ENDPOINTS REST: PANEL ADMINISTRADOR Y CONTROL DE NEGOCIO
# ==============================================================================

@app.route('/admin')
def ruta_admin_html():
    """Sirve la vista /admin exclusiva de administración."""
    admin_path = os.path.join(BASE_DIR, "admin.html")
    if os.path.exists(admin_path):
        return send_file(admin_path)
    return jsonify({"status": "error", "message": "admin.html no encontrado"}), 404

@app.route('/api/admin/calculate-profitability', methods=['POST'])
@require_admin
def api_admin_calcular():
    """Algoritmo de cálculo de rentabilidad para margen del 70%."""
    data = request.json or {}
    minutos = float(data.get("minutos", 100))
    costo_por_millon = float(data.get("costo_por_millon", API_COST_PER_MILLION_TOKENS))
    vision_por_min = int(data.get("vision_por_min", DEFAULT_VISION_TOKENS_PER_MIN))
    texto_por_min = int(data.get("texto_por_min", DEFAULT_TEXT_TOKENS_PER_MIN))
    margen_objetivo = float(data.get("margen_objetivo", TARGET_GROSS_MARGIN_PERCENT))

    resultado = calcular_rentabilidad(minutos, costo_por_millon, vision_por_min, texto_por_min, margen_objetivo)
    return jsonify({"status": "success", "data": resultado}), 200

@app.route('/api/admin/user/update', methods=['POST'])
@require_admin
def api_admin_update_user():
    """Actualiza rol, tokens, minutos y api_key de un usuario con validación estricta de seguridad."""
    data = request.json or {}
    uid = data.get("uid")
    target_email = data.get("target_email", "").strip()
    new_role = data.get("role", "free")
    tokens_total = data.get("tokens_total")
    minutes_total = data.get("minutes_total")
    minutes_used = data.get("minutes_used")
    assigned_free_key_name = data.get("assigned_free_key_name", "")
    assigned_free_key_id = data.get("assigned_free_key_id", "")
    api_key = data.get("api_key", "")

    if not uid or not target_email:
        return jsonify({"status": "error", "message": "Faltan parámetros obligatorios (uid, target_email)"}), 400

    # Restricción estricta: Bloquear asignación de 'admin' a cualquier otro correo
    try:
        assert_no_unauthorized_admin(target_email, new_role)
    except PermissionError as pe:
        return jsonify({"status": "error", "message": str(pe)}), 403

    effective_role = "admin" if target_email.lower() == ADMIN_EMAIL else new_role
    if tokens_total is not None:
        effective_total = int(tokens_total)
    elif effective_role == "admin":
        effective_total = 999999999
    elif effective_role == "premium":
        effective_total = PLAN_PRO_TOKENS
    else:
        effective_total = PLAN_FREE_TOKENS

    effective_min_total = float(minutes_total) if minutes_total is not None else (
        999999.0 if effective_role == "admin" else (180.0 if effective_role == "premium" else 10.0)
    )

    try:
        raw_u = redis_client.get(f"user_data:{uid}")
        u_data = json.loads(raw_u.decode('utf-8')) if raw_u else {}
        u_data.update({
            "uid": uid,
            "email": target_email,
            "role": effective_role,
            "tokens_total": effective_total,
            "minutes_total": effective_min_total,
            "assigned_free_key_name": assigned_free_key_name if assigned_free_key_name is not None else u_data.get("assigned_free_key_name", ""),
            "assigned_free_key_id": assigned_free_key_id if assigned_free_key_id is not None else u_data.get("assigned_free_key_id", ""),
            "api_key": api_key if api_key else u_data.get("api_key", ""),
            "tokens_used": u_data.get("tokens_used", 0),
            "minutes_used": float(minutes_used) if minutes_used is not None else float(u_data.get("minutes_used", 0.0)),
            "last_reset_at": u_data.get("last_reset_at", int(time.time()))
        })
        redis_client.set(f"user_data:{uid}", json.dumps(u_data), ex=86400 * 30)
    except Exception as e:
        print(f"[Redis Warning] {e}")

    return jsonify({
        "status": "success",
        "message": f"Usuario {target_email} actualizado con rol {effective_role}, cuota de {effective_min_total:.1f} min ({effective_total:,} tokens)."
    }), 200

@app.route('/api/admin/user/reset-tokens', methods=['POST'])
@require_admin
def api_admin_reset_tokens():
    """Reinicia a 0 los tokens y minutos consumidos de un usuario para un nuevo ciclo de facturación."""
    data = request.json or {}
    uid = data.get("uid")
    if not uid:
        return jsonify({"status": "error", "message": "Falta parámetro uid"}), 400

    try:
        raw_u = redis_client.get(f"user_data:{uid}")
        if raw_u:
            u_data = json.loads(raw_u.decode('utf-8'))
            u_data["tokens_used"] = 0
            u_data["minutes_used"] = 0.0
            u_data["last_reset_at"] = int(time.time())
            redis_client.set(f"user_data:{uid}", json.dumps(u_data), ex=86400 * 30)
    except Exception as e:
        print(f"[Redis Warning] {e}")

    return jsonify({"status": "success", "message": "Contador de minutos y tokens reiniciado a 0 exitosamente."}), 200

def construir_planes_publicos():
    """Devuelve los planes con su equivalencia real en minutos y margen."""
    pro = calcular_rentabilidad(PLAN_PRO_MINUTES)
    custom = calcular_rentabilidad(PLAN_CUSTOM_MINUTES)

    def margen_real(precio, costo):
        return round(((precio - costo) / precio) * 100.0, 1) if precio > 0 else 0

    return {
        "free": {
            "nombre": "Gratis",
            "precio_usd": 0,
            "tokens": PLAN_FREE_TOKENS,
            "minutos_estimados": round(PLAN_FREE_TOKENS / float(DEFAULT_VISION_TOKENS_PER_MIN + DEFAULT_TEXT_TOKENS_PER_MIN), 2),
            "modelo": OPENROUTER_FREE_MODEL,
            "usa_api_key_openai": False
        },
        "pro": {
            "nombre": "Pro",
            "precio_usd": PLAN_PRO_PRICE_USD,
            "tokens": PLAN_PRO_TOKENS,
            "minutos": PLAN_PRO_MINUTES,
            "costo_api_usd": pro["costo_api_usd"],
            "ganancia_bruta_usd": round(PLAN_PRO_PRICE_USD - pro["costo_api_usd"], 2),
            "margen_bruto_porcentaje": margen_real(PLAN_PRO_PRICE_USD, pro["costo_api_usd"]),
            "precio_minimo_50_margen_usd": pro["precio_min_sugerido_usd"],
            "precio_por_minuto_usd": round(PLAN_PRO_PRICE_USD / PLAN_PRO_MINUTES, 4),
            "costo_por_minuto_usd": pro["costo_por_minuto_usd"],
            "minutos_extra_precio_usd": PLAN_OVERAGE_PRICE_PER_MIN
        },
        "custom": {
            "nombre": "Empresarial",
            "precio_desde_usd": PLAN_CUSTOM_PRICE_USD,
            "tokens": PLAN_CUSTOM_TOKENS,
            "minutos": PLAN_CUSTOM_MINUTES,
            "costo_api_usd": custom["costo_api_usd"],
            "ganancia_bruta_usd": round(PLAN_CUSTOM_PRICE_USD - custom["costo_api_usd"], 2),
            "margen_bruto_porcentaje": margen_real(PLAN_CUSTOM_PRICE_USD, custom["costo_api_usd"])
        },
        "supuestos": {
            "vision_tokens_por_min": DEFAULT_VISION_TOKENS_PER_MIN,
            "texto_tokens_por_min": DEFAULT_TEXT_TOKENS_PER_MIN,
            "tokens_por_min": DEFAULT_VISION_TOKENS_PER_MIN + DEFAULT_TEXT_TOKENS_PER_MIN,
            "costo_por_millon_tokens_usd": API_COST_PER_MILLION_TOKENS,
            "margen_objetivo_porcentaje": TARGET_GROSS_MARGIN_PERCENT
        }
    }



# ==============================================================================
# ENDPOINTS REST: POOL DE CLAVES GRATUITAS Y GESTIÓN
# ==============================================================================
@app.route('/api/admin/free-keys', methods=['GET'])
@require_admin
def api_admin_get_free_keys():
    pool = obtener_pool_claves_gratuitas()
    return jsonify({
        "status": "success",
        "keys": pool
    }), 200

@app.route('/api/admin/free-keys', methods=['POST'])
@require_admin
def api_admin_post_free_keys():
    data = request.json or {}
    action = data.get("action", "add")
    pool = obtener_pool_claves_gratuitas()

    if action == "sync":
        new_keys = data.get("keys", [])
        if isinstance(new_keys, list):
            for k in new_keys:
                k_mod = (k.get("model") or "").strip()
                if not k_mod or k_mod == "gemini-3.5-flash-lite" or "gemini" in k_mod.lower():
                    k["model"] = "gemini-3.5-flash-lite"
                    k["base_url"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
            pool = new_keys
            guardar_pool_claves_gratuitas(pool)
            return jsonify({"status": "success", "message": f"{len(pool)} claves sincronizadas con éxito.", "keys": pool}), 200

    elif action == "add":
        name = (data.get("name") or "").strip()
        key = (data.get("api_key") or "").strip()
        model = (data.get("model") or "gemini-3.5-flash-lite").strip()
        base_url = (data.get("base_url") or "https://generativelanguage.googleapis.com/v1beta/openai/").strip()

        if not name or not key:
            return jsonify({"status": "error", "message": "El nombre y la Clave de API son obligatorios."}), 400

        # Para cualquier clave registrada con modelo gemini-3.5-flash-lite, fija estrictamente su base_url y model
        if model == "gemini-3.5-flash-lite" or "gemini" in model.lower():
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
            model = "gemini-3.5-flash-lite"

        new_entry = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "api_key": key,
            "model": model,
            "base_url": base_url,
            "status": "active",
            "uses_count": 0,
            "last_error": None,
            "created_at": int(time.time())
        }
        pool.append(new_entry)
        guardar_pool_claves_gratuitas(pool)
        return jsonify({"status": "success", "message": f"Clave '{name}' añadida con éxito ({model}).", "key": new_entry, "keys": pool}), 201

    elif action == "toggle_status":
        key_id = data.get("id")
        target_status = data.get("status")
        found = False
        for k in pool:
            if k.get("id") == key_id or k.get("name") == key_id:
                k["status"] = target_status or ("suspended" if k.get("status") == "active" else "active")
                if k["status"] == "active":
                    k["last_error"] = None
                found = True
                break
        if found:
            guardar_pool_claves_gratuitas(pool)
            return jsonify({"status": "success", "message": "Estado de la clave actualizado.", "keys": pool}), 200
        return jsonify({"status": "error", "message": "Clave no encontrada."}), 404

    elif action == "delete":
        key_id = data.get("id")
        pool = [k for k in pool if k.get("id") != key_id and k.get("name") != key_id]
        guardar_pool_claves_gratuitas(pool)
        return jsonify({"status": "success", "message": "Clave eliminada del pool.", "keys": pool}), 200

    elif action == "test":
        key_val = (data.get("api_key") or "").strip()
        key_id = data.get("id")
        key_name = (data.get("name") or "").strip()
        model = (data.get("model") or "").strip()
        base_url = (data.get("base_url") or "").strip()

        if not key_val and key_id:
            for k in pool:
                if k.get("id") == key_id or k.get("name") == key_id:
                    key_val = k.get("api_key", "")
                    key_name = k.get("name", "")
                    model = k.get("model", model)
                    base_url = k.get("base_url", base_url)
                    break

        if not key_val:
            return jsonify({"status": "error", "message": "No se proporcionó API key para probar."}), 400

        resolved_url, resolved_model = resolver_config_clave(key_val, key_name, model=model, base_url=base_url)

        headers = {}
        if "openrouter" in resolved_url or key_val.startswith("sk-or-"):
            headers = {"HTTP-Referer": OPENROUTER_REFERER, "X-Title": OPENROUTER_TITLE}

        try:
            t_client = OpenAI(
                base_url=resolved_url,
                api_key=key_val,
                default_headers=headers if headers else None,
                http_client=httpx.Client(trust_env=False, timeout=15.0)
            )
            resp = t_client.chat.completions.create(
                model=resolved_model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hola, prueba rapida de conexion."}]
            )
            res_text = resp.choices[0].message.content.strip() if resp.choices else "OK"
            return jsonify({"status": "success", "message": f"Conexión exitosa con el modelo {resolved_model}: {res_text[:60]}", "model": resolved_model}), 200
        except Exception as te:
            return jsonify({"status": "error", "message": f"Fallo al conectar con la API ({resolved_model}): {str(te)}"}), 400

    return jsonify({"status": "error", "message": f"Acción desconocida: {action}"}), 400

@app.route('/api/admin/keys', methods=['GET', 'POST'])
@require_admin
def api_admin_keys():
    """Permite al Superadministrador consultar y actualizar las API keys del servidor dinámicamente."""
    global OPENAI_API_KEY, OPENROUTER_API_KEY, OPENROUTER_FREE_MODEL

    if request.method == 'POST':
        data = request.json or {}
        new_openai = (data.get("openai_api_key") or "").strip()
        new_or = (data.get("openrouter_api_key") or "").strip()
        new_model = (data.get("free_model") or "").strip()

        if "openai_api_key" in data:
            new_openai = (data.get("openai_api_key") or "").strip()
            if new_openai and "test-key" not in new_openai:
                OPENAI_API_KEY = new_openai
                try:
                    redis_client.set("config:openai_api_key", new_openai)
                except Exception as e:
                    print(f"[Redis Warning] {e}")
            else:
                OPENAI_API_KEY = ""
                try:
                    redis_client.delete("config:openai_api_key")
                except Exception as e:
                    print(f"[Redis Warning] {e}")

        if new_or:
            OPENROUTER_API_KEY = new_or
            try:
                redis_client.set("config:openrouter_api_key", new_or)
            except Exception as e:
                print(f"[Redis Warning] {e}")

        if new_model:
            OPENROUTER_FREE_MODEL = new_model
            try:
                redis_client.set("config:openrouter_free_model", new_model)
            except Exception as e:
                print(f"[Redis Warning] {e}")

        return jsonify({
            "status": "success",
            "message": "Claves de IA guardadas y activadas con éxito en el servidor.",
            "has_openai_key": bool(OPENAI_API_KEY),
            "has_openrouter_key": bool(OPENROUTER_API_KEY),
            "free_model": OPENROUTER_FREE_MODEL
        }), 200

    # GET
    recargar_claves_desde_redis()
    openai_preview = f"sk-...{OPENAI_API_KEY[-4:]}" if len(OPENAI_API_KEY) > 8 else ("Configurada" if OPENAI_API_KEY else "")
    or_preview = f"sk-or-...{OPENROUTER_API_KEY[-4:]}" if len(OPENROUTER_API_KEY) > 8 else ("Configurada" if OPENROUTER_API_KEY else "")

    return jsonify({
        "status": "success",
        "has_openai_key": bool(OPENAI_API_KEY),
        "openai_preview": openai_preview,
        "has_openrouter_key": bool(OPENROUTER_API_KEY),
        "openrouter_preview": or_preview,
        "free_model": OPENROUTER_FREE_MODEL
    }), 200

@app.route('/api/plans', methods=['GET'])
def api_planes_publicos():
    """Planes públicos con tokens, minutos y margen. Sin datos sensibles."""
    return jsonify({"status": "success", "planes": construir_planes_publicos()}), 200

@app.route('/api/admin/config', methods=['GET'])
def api_admin_config():
    """Configuración pública y parámetros de la plataforma."""
    return jsonify({
        "status": "success",
        "admin_email": ADMIN_EMAIL,
        "free_model": OPENROUTER_FREE_MODEL,
        "fallback_model": OPENROUTER_FALLBACK_MODEL,
        "tokens_per_min": {
            "text": DEFAULT_TEXT_TOKENS_PER_MIN,
            "vision": DEFAULT_VISION_TOKENS_PER_MIN
        },
        "target_margin": TARGET_GROSS_MARGIN_PERCENT,
        "api_cost_per_million": API_COST_PER_MILLION_TOKENS,
        "planes": construir_planes_publicos()
    }), 200

# ==============================================================================
# ENDPOINTS REST: PROCESAMIENTO MULTIMEDIA Y PIPELINE
# ==============================================================================

def obtener_api_key_servidor(uid, role):
    """
    Resuelve la API key en el servidor con respaldo en Redis.
    - Superadmin: OPENAI_API_KEY del servidor (o de Redis config:openai_api_key).
    - Free: OPENROUTER_API_KEY del servidor (o de Redis config:openrouter_api_key).
    - Pro: Clave individual del usuario; si no tiene, usa la OPENAI_API_KEY del servidor.
    """
    global OPENAI_API_KEY, OPENROUTER_API_KEY
    recargar_claves_desde_redis()

    if role == "free":
        if OPENROUTER_API_KEY:
            return OPENROUTER_API_KEY
        if uid:
            try:
                raw_u = redis_client.get(f"user_data:{uid}")
                if raw_u:
                    u_data = json.loads(raw_u.decode("utf-8"))
                    key = (u_data.get("api_key") or "").strip()
                    if key and key.startswith("sk-or-"):
                        return key
            except Exception:
                pass
        return ""

    if role == "admin":
        if OPENAI_API_KEY:
            return OPENAI_API_KEY
        if uid:
            try:
                raw_u = redis_client.get(f"user_data:{uid}")
                if raw_u:
                    u_data = json.loads(raw_u.decode("utf-8"))
                    key = (u_data.get("api_key") or "").strip()
                    if key:
                        return key
            except Exception:
                pass
        return ""

    # Rol Pro (premium)
    if uid:
        try:
            raw_u = redis_client.get(f"user_data:{uid}")
            if raw_u:
                u_data = json.loads(raw_u.decode("utf-8"))
                key = (u_data.get("api_key") or "").strip()
                if key:
                    return key
        except Exception as e:
            print(f"[API Key Warning] No se pudo leer la clave de {uid}: {e}")

    if OPENAI_API_KEY:
        return OPENAI_API_KEY

    return ""

@app.route('/procesar-fase1', methods=['POST'])
def api_fase1():
    try:
        data = request.json or {}
        minutes_used = float(data.get("minutes_used", 0.0))
        minutes_total = float(data.get("minutes_total", 10.0))
        video_url = data.get("video_url")
        proyecto_id = data.get("proyecto_id")
        uid = data.get("uid")
        user_email = data.get("user_email")
        role = data.get("role", "free")
        tokens_used = int(data.get("tokens_used", 0))
        tokens_total = int(data.get("tokens_total", PLAN_FREE_TOKENS))

        if not video_url or not proyecto_id:
            return jsonify({"status": "error", "message": "Faltan parámetros obligatorios: video_url, proyecto_id"}), 400

        # Garantizar rol de superadministrador si coincide el correo
        if user_email and user_email.strip().lower() == ADMIN_EMAIL:
            role = "admin"
        elif role == "admin":
            role = "free"

        # Resolver clave del servidor, permitiendo clave del administrador si viene en la petición
        client_key = (data.get("api_key") or "").strip()
        api_key = ""
        if role == "admin":
            if client_key and client_key.startswith("sk-"):
                api_key = client_key
                # Sincronizar de inmediato en Redis y memoria del servidor
                try:
                    redis_client.set("config:openai_api_key", client_key)
                    global OPENAI_API_KEY
                    OPENAI_API_KEY = client_key
                    print("[API Key] Clave de Superadmin sincronizada en Redis.")
                except Exception as e_red:
                    print(f"[Redis Warning] {e_red}")
            else:
                api_key = obtener_api_key_servidor(uid, role)
        else:
            api_key = obtener_api_key_servidor(uid, role)
            if not api_key and client_key and client_key.startswith("sk-"):
                api_key = client_key

        if role == "premium" and not api_key:
            msg = ("Tu plan Pro aún no tiene una API key asignada. "
                   "El administrador debe asignarla desde el panel de administración antes de procesar videos.")
            guardar_fase1_status(proyecto_id, "error", msg)
            return jsonify({"status": "error", "message": msg}), 400

        if role == "admin" and not api_key:
            msg = "El servidor no tiene configurada la OPENAI_API_KEY. Como Superadmin, puedes ingresarla directamente desde el Panel de Administrador (sección Configuración de IA)."
            guardar_fase1_status(proyecto_id, "error", msg)
            return jsonify({"status": "error", "message": msg}), 400

        if role == "free":
            if not api_key:
                api_key = "free_user"
            # Verificar si ya alcanzó su límite de minutos
            cur_min_u = minutes_used
            cur_min_t = minutes_total
            if uid:
                try:
                    raw_u = redis_client.get(f"user_data:{uid}")
                    if raw_u:
                        u_chk = json.loads(raw_u.decode('utf-8'))
                        cur_min_u = float(u_chk.get("minutes_used", cur_min_u) or 0.0)
                        cur_min_t = float(u_chk.get("minutes_total", cur_min_t) or 10.0)
                except Exception:
                    pass
            if cur_min_u >= cur_min_t:
                msg = f"Has alcanzado tu límite de {cur_min_t:.1f} minutos de video permitidos en la versión gratuita. Actualiza a Pro para audiodescribir más videos."
                guardar_fase1_status(proyecto_id, "quota_exceeded", msg)
                return jsonify({"status": "quota_exceeded", "message": msg}), 400

        print(f"[Info] Encolando Fase 1: proyecto={proyecto_id}, email={user_email}, role={role}")
        guardar_fase1_status(proyecto_id, "processing")

        # Encolar en Celery con auto-recuperación en caso de VersionMismatch de redis
        enqueued = False
        selected_voice = data.get("voice", "es-ES-AlvaroNeural")
        modo = data.get("modo", "audiodescripcion")
        ocr_enabled = bool(data.get("ocr_enabled", modo == "texto_a_voz"))

        for attempt in range(2):
            try:
                procesar_video_task.apply_async(
                    args=[video_url, api_key, proyecto_id, user_email, uid, role, tokens_used, tokens_total],
                    kwargs={"voice": selected_voice, "ocr_enabled": ocr_enabled, "modo": modo}
                )
                enqueued = True
                break
            except Exception as e_cel:
                err_text = str(e_cel).lower()
                print(f"[Celery Enqueue Warning] Intento {attempt + 1}: {e_cel}")
                if "versionmismatch" in err_text or "redis" in err_text:
                    print("[Celery Fix] Actualizando redis a >=6.1.0 en caliente...")
                    try:
                        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "redis>=6.1.0"])
                        import redis
                        import importlib
                        importlib.reload(redis)
                    except Exception as e_up:
                        print(f"[Hot Update Error] {e_up}")
                    continue
                else:
                    try:
                        procesar_video_task.delay(video_url, api_key, proyecto_id)
                        enqueued = True
                        break
                    except Exception as e_simp:
                        print(f"[Celery Simple Call Error] {e_simp}")
                        break

        if not enqueued:
            raise RuntimeError("No se pudo encolar la tarea en Celery (desajuste de versiones en el worker). El contenedor se está actualizando automáticamente.")

        return jsonify({
            "status": "processing",
            "message": "El análisis ha comenzado de fondo.",
            "role": role,
            "proyecto_id": proyecto_id
        }), 202

    except Exception as e:
        import traceback
        err_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[Error en /procesar-fase1] {err_msg}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Error interno en el servidor: {err_msg}"}), 500

@app.route('/fase1-progreso', methods=['GET'])
def api_fase1_progreso():
    """Retorna el progreso en tiempo real de la Fase 1 con los bloques generados hasta el momento."""
    proyecto_id = request.args.get("proyecto_id")
    if not proyecto_id:
        return jsonify({"status": "error", "message": "Falta proyecto_id"}), 400

    try:
        raw = redis_client.get(f"fase1_progreso:{proyecto_id}")
        if raw:
            return jsonify(json.loads(raw.decode('utf-8'))), 200

        # Si aún no hay progreso en Redis, checar fase1_status
        raw_st = redis_client.get(f"fase1_status:{proyecto_id}")
        if raw_st:
            st = json.loads(raw_st.decode('utf-8'))
            return jsonify({
                "status": st.get("status", "processing"),
                "current_scene": 0,
                "total_scenes": 0,
                "percent": 0,
                "bloques": []
            }), 200

        return jsonify({"status": "processing", "current_scene": 0, "total_scenes": 0, "percent": 0, "bloques": []}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/fase1-status', methods=['GET'])
def api_fase1_status():
    proyecto_id = request.args.get("proyecto_id")
    if not proyecto_id:
        return jsonify({"status": "error", "message": "Falta el parámetro proyecto_id"}), 400
    
    status, error_msg = obtener_fase1_status(proyecto_id)
    return jsonify({"status": status, "error": error_msg}), 200

@app.route('/fase1-resultado', methods=['GET'])
def api_fase1_resultado():
    proyecto_id = request.args.get("proyecto_id")
    if not proyecto_id:
        return jsonify({"status": "error", "message": "Falta el parámetro proyecto_id"}), 400

    try:
        val = redis_client.get(f"fase1_resultado:{proyecto_id}")
        if not val:
            return jsonify({"status": "error", "message": "Resultado no disponible o aún en proceso."}), 404
        data = json.loads(val.decode('utf-8'))
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error al leer resultado de Redis: {str(e)}"}), 500

@app.route('/fase1-start', methods=['POST'])
def api_fase1_start():
    data = request.json or {}
    proyecto_id = data.get("proyecto_id")
    if proyecto_id:
        guardar_fase1_status(proyecto_id, "processing")
    return jsonify({"status": "processing"}), 202

@app.route('/api/tts', methods=['GET'])
def api_tts():
    texto = request.args.get("text", "").strip()
    voz = request.args.get("voice", "es-ES-AlvaroNeural").strip()
    rate = request.args.get("rate", "+0%").strip()
    if not rate or rate in ["0%", "0", " 0%"]:
        rate = "+0%"
    elif not (rate.startswith("+") or rate.startswith("-")):
        rate = f"+{rate}" 

    if not texto:
        return "Texto no proporcionado", 400

    ruta_cache = obtener_ruta_cache_tts(texto, voz, rate)
    if os.path.exists(ruta_cache) and os.path.getsize(ruta_cache) > 0:
        optimizar_mp3_firefox(ruta_cache)
        return send_file(ruta_cache, mimetype="audio/mpeg")

    path_audio = os.path.join(TEMP_DIR, f"tts_{uuid.uuid4().hex}.mp3")
    exito, errores = generar_tts_robusto(texto, voz, path_audio, rate)
    if not exito:
        return f"Error al generar síntesis de voz edge-tts: {errores}", 500
            
    return send_file(path_audio, mimetype="audio/mpeg")

@app.route('/renderizar-audio-fase2', methods=['POST'])
def api_renderizar_audio_fase2():
    """
    Renderiza la mezcla final de audio (.mp3) combinando el audio original del video
    con las pistas de audiodescripción sincronizadas según la edición actual del editor.
    """
    data = request.json or {}
    video_url = data.get("video_url")
    proyecto_id = data.get("proyecto_id")
    bloques_editados = data.get("bloques", [])

    if not video_url or not proyecto_id:
        return jsonify({"status": "error", "message": "Faltan parámetros obligatorios: video_url, proyecto_id"}), 400

    video_path = os.path.join(TEMP_DIR, f"{proyecto_id}_original.mp4")
    ruta_mezcla_mp3 = os.path.join(TEMP_DIR, f"{proyecto_id}_mezcla_ad_final.mp3")

    try:
        if not os.path.exists(video_path):
            descargar_video(video_url, video_path)

        video_original = VideoFileClip(video_path)
        audio_original = video_original.audio
        pistas_ad = []
        rutas_archivos_ad = []

        for i, bloque in enumerate(bloques_editados):
            texto = bloque.get("text", "").strip()
            inicio_clip = float(bloque.get("start", 0))
            voz_id = bloque.get("voice", "es-ES-AlvaroNeural")
            rate = bloque.get("rate", "+0%").strip()

            if not texto:
                continue

            path_audio = generar_audio_pro(texto, i, voz_id, proyecto_id=proyecto_id, rate=rate)
            if not path_audio:
                continue

            rutas_archivos_ad.append(path_audio)
            ad_clip = AudioFileClip(path_audio)
            ad_clip = ad_clip.set_start(inicio_clip)
            pistas_ad.append(ad_clip)

        if not pistas_ad:
            return jsonify({"status": "error", "message": "No hay pistas de audiodescripción para renderizar."}), 400
        else:
            # PISTA PURA Y EXCLUSIVA DE AUDIODESCRIPCIÓN (Solo voz IA sincronizada, con silencios digitales continuos)
            duracion_total = video_original.duration if (video_original and video_original.duration) else 0
            if not duracion_total or duracion_total <= 0:
                duracion_total = max([float(b.get("end", 0)) for b in bloques_editados] + [10.0])

            def generar_silencio_digital(t):
                if isinstance(t, np.ndarray):
                    return np.zeros((len(t), 2))
                return np.zeros(2)

            pista_base_silencio = AudioClip(generar_silencio_digital, duration=duracion_total)
            pista_base_silencio.fps = 44100

            audio_final = CompositeAudioClip([pista_base_silencio] + pistas_ad)
            audio_final.duration = duracion_total
            audio_final.fps = 44100

        if audio_final is not None:
            audio_final.write_audiofile(ruta_mezcla_mp3, fps=44100, verbose=False, logger=None)
        else:
            return jsonify({"status": "error", "message": "No se pudo generar la mezcla de audio."}), 500

        video_original.close()
        for a in pistas_ad:
            try:
                a.close()
            except:
                pass

        for path_a in rutas_archivos_ad:
            try:
                if os.path.exists(path_a):
                    os.remove(path_a)
            except:
                pass

        return send_file(
            ruta_mezcla_mp3,
            as_attachment=True,
            download_name=f"audiodescripcion_{proyecto_id}.mp3",
            mimetype="audio/mpeg"
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Error al renderizar audio: {str(e)}"}), 500

@app.route('/renderizar-fase2', methods=['POST'])
def api_fase2():
    data = request.json or {}
    video_url = data.get("video_url")
    proyecto_id = data.get("proyecto_id")
    bloques_editados = data.get("bloques")

    if not video_url or not proyecto_id or not bloques_editados:
        return jsonify({"status": "error", "message": "Faltan parámetros obligatorios: video_url, proyecto_id, bloques"}), 400

    video_path = os.path.join(TEMP_DIR, f"{proyecto_id}_original.mp4")
    
    try:
        if not os.path.exists(video_path):
            descargar_video(video_url, video_path)

        video_original = VideoFileClip(video_path)
        audio_original = video_original.audio
        pistas_ad = []
        rutas_archivos_ad = []
        ruta_mezcla_mp3 = os.path.join(TEMP_DIR, f"{proyecto_id}_mezcla_ad_final.mp3")

        for i, bloque in enumerate(bloques_editados):
            texto = bloque.get("text", "").strip()
            inicio_clip = float(bloque.get("start", 0))
            voz_id = bloque.get("voice", "es-ES-AlvaroNeural")
            rate = bloque.get("rate", "+0%").strip()

            if not texto:
                continue

            path_audio = generar_audio_pro(texto, i, voz_id, proyecto_id=proyecto_id, rate=rate)
            if not path_audio:
                continue

            rutas_archivos_ad.append(path_audio)
            ad_clip = AudioFileClip(path_audio)
            ad_clip = ad_clip.set_start(inicio_clip)
            pistas_ad.append(ad_clip)

        if not pistas_ad:
            audio_final = audio_original
        else:
            audio_tracks = []
            if audio_original is not None:
                audio_tracks.append(audio_original.volumex(0.6))
            audio_tracks.extend([a.volumex(1.5) for a in pistas_ad])
            
            if audio_tracks:
                audio_final = CompositeAudioClip(audio_tracks)
                audio_final.duration = video_original.duration
            else:
                audio_final = None

        video_final = video_original.set_audio(audio_final) if audio_final else video_original
        output_path = os.path.join(TEMP_DIR, f"{proyecto_id}_AD_PRO.mp4")

        try:
            video_final.write_videofile(
                output_path,
                codec="h264_nvenc",
                audio_codec="aac",
                preset="fast",
                verbose=False,
                logger=None
            )
        except Exception as e_gpu:
            print(f"[Warning] GPU NVENC falló ({e_gpu}). Usando CPU (libx264)...")
            video_final.write_videofile(
                output_path,
                codec="libx264",
                audio_codec="aac",
                preset="fast",
                verbose=False,
                logger=None
            )

        try:
            audio_final.write_audiofile(ruta_mezcla_mp3, verbose=False, logger=None)
        except Exception as e_audio:
            print(f"[Warning] No se pudo exportar la mezcla mp3: {e_audio}")

        video_original.close()
        for a in pistas_ad:
            try:
                a.close()
            except:
                pass

        for path_a in rutas_archivos_ad:
            try:
                if os.path.exists(path_a):
                    os.remove(path_a)
            except Exception as e_del:
                print(f"[Warning] No se pudo eliminar audio temporal {path_a}: {e_del}")

        return jsonify({
            "status": "success",
            "proyecto_id": proyecto_id,
            "bloques_procesados": len(pistas_ad)
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/descargar-audio', methods=['GET'])
def descargar_audio():
    proyecto_id = request.args.get("proyecto_id")
    if not proyecto_id:
        return jsonify({"status": "error", "message": "Falta el parámetro proyecto_id"}), 400

    ruta_mp3 = os.path.join(TEMP_DIR, f"{proyecto_id}_mezcla_ad_final.mp3")
    if not os.path.exists(ruta_mp3):
        return jsonify({"status": "error", "message": "No hay mezcla de audio disponible para este proyecto. Renderiza primero."}), 404
    return send_file(ruta_mp3, as_attachment=True, download_name="audiodescripcion_final.mp3", mimetype="audio/mpeg")

if __name__ == '__main__':
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)