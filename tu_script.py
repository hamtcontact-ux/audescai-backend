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
from functools import wraps

# Forzar actualización de edge-tts en el arranque para evitar error 403 de Microsoft
try:
    print("[Startup] Intentando forzar actualización de edge-tts a >=6.1.12...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "edge-tts>=6.1.12"])
    print("[Startup] Actualización de edge-tts completada exitosamente.")
except Exception as e:
    print(f"[Startup Warning] No se pudo forzar la actualización de edge-tts: {e}")

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
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
OPENROUTER_FREE_MODEL = os.environ.get("OPENROUTER_FREE_MODEL", "google/gemini-2.0-flash-exp:free").strip()
OPENROUTER_FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "meta-llama/llama-3.3-70b-instruct:free").strip()
OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "https://cineparaciegos.com").strip()
OPENROUTER_TITLE = os.environ.get("OPENROUTER_TITLE", "CineParaCiegos AudescAI").strip()

# Clave de OpenAI reservada para el servidor.
# NUNCA debe enviarse al frontend ni guardarse en el navegador del usuario.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Parámetros del Algoritmo de Rentabilidad
DEFAULT_TEXT_TOKENS_PER_MIN = int(os.environ.get("DEFAULT_TEXT_TOKENS_PER_MIN", "200"))
DEFAULT_VISION_TOKENS_PER_MIN = int(os.environ.get("DEFAULT_VISION_TOKENS_PER_MIN", "30000"))
TARGET_GROSS_MARGIN_PERCENT = float(os.environ.get("TARGET_GROSS_MARGIN_PERCENT", "50.0"))
API_COST_PER_MILLION_TOKENS = float(os.environ.get("API_COST_PER_MILLION_TOKENS", "2.50"))

# Planes comerciales (fuente única de verdad para backend y UI)
PLAN_FREE_TOKENS = int(os.environ.get("PLAN_FREE_TOKENS", "300000"))
PLAN_PRO_TOKENS = int(os.environ.get("PLAN_PRO_TOKENS", "5436000"))
PLAN_PRO_MINUTES = int(os.environ.get("PLAN_PRO_MINUTES", "180"))
PLAN_PRO_PRICE_USD = float(os.environ.get("PLAN_PRO_PRICE_USD", "29"))
PLAN_CUSTOM_TOKENS = int(os.environ.get("PLAN_CUSTOM_TOKENS", "18120000"))
PLAN_CUSTOM_MINUTES = int(os.environ.get("PLAN_CUSTOM_MINUTES", "600"))
PLAN_CUSTOM_PRICE_USD = float(os.environ.get("PLAN_CUSTOM_PRICE_USD", "99"))
PLAN_OVERAGE_PRICE_PER_MIN = float(os.environ.get("PLAN_OVERAGE_PRICE_PER_MIN", "0.19"))

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from openai import OpenAI
import httpx
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip
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
            OPENAI_API_KEY = ck_openai.decode('utf-8').strip()
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

# Función para descargar un archivo de internet
def descargar_video(url, dest_path):
    print(f"[Info] Descargando video desde {url} hacia {dest_path}")
    urllib.request.urlretrieve(url, dest_path)
    print("[Info] Descarga completada.")

# --- 1. DETECCIÓN DE HUECOS (VAD) ---
def obtener_huecos_silencio(video_path, min_gap=2.2, max_intervalo=6.0):
    print(f"[Info] Analizando diálogos en: {os.path.basename(video_path)}")
    vad_model = load_silero_vad()
    video = VideoFileClip(video_path)
    
    audio_temp = os.path.join(TEMP_DIR, f"temp_vad_{uuid.uuid4().hex}.wav")
    video.audio.write_audiofile(audio_temp, fps=16000, nbytes=2, codec='pcm_s16le', verbose=False, logger=None)
    
    wav = read_audio(audio_temp, sampling_rate=16000)
    speech_ts = get_speech_timestamps(wav, vad_model, sampling_rate=16000)
    
    duracion_total = video.duration
    silencios = []
    cursor = 0.0
    
    for ts in speech_ts:
        inicio_voz = ts['start'] / 16000
        if inicio_voz - cursor >= min_gap:
            silencios.append((cursor, inicio_voz))
        cursor = ts['end'] / 16000
        
    if duracion_total - cursor >= min_gap:
        silencios.append((cursor, duracion_total))
        
    video.close()
    
    try:
        os.remove(audio_temp)
    except:
        pass

    huecos_subdivididos = []
    for inicio, fin in silencios:
        cursor_trozo = inicio
        while fin - cursor_trozo > max_intervalo * 1.3:
            huecos_subdivididos.append((cursor_trozo, cursor_trozo + max_intervalo))
            cursor_trozo += max_intervalo
        huecos_subdivididos.append((cursor_trozo, fin))

    return huecos_subdivididos

# --- 2. ANÁLISIS CON OPENAI / OPENROUTER ---
def analizar_con_openai(openai_client, video_path, inicio, fin, indice_escena, historial, model_name="gpt-4o-mini"):
    duracion_max = fin - inicio
    print(f"   [Info] Analizando escena {indice_escena} ({model_name}): {inicio:.1f}s a {fin:.1f}s")
    
    frame_path = os.path.join(TEMP_DIR, f"frame_desc_{uuid.uuid4().hex}.jpg")
    with VideoFileClip(video_path) as video:
        t_frame = max(0.1, inicio + 0.5)
        video.save_frame(frame_path, t=min(t_frame, video.duration - 0.1))

    base64_image = encode_image(frame_path)
    palabras = max(15, int(duracion_max * 2.2))
    
    instruccion_escena = (
        "OBLIGATORIO: Al ser la primera escena del video, DEBES empezar tu descripción indicando el LUGAR (y DÍA/NOCHE solo si es inconfundible), seguido de la acción (ej: 'CALLE. El hombre...', o 'NOCHE, EXTERIOR.')." 
        if indice_escena == 0 else 
        "PROHIBIDO NOMBRAR EL LUGAR O CLIMA si estás en el mismo entorno del HISTORIAL. Ve directo a la acción. SOLO si el LUGAR cambia drásticamente respecto al historial, menciónalo al principio."
    )
    historial_str = " | ".join(historial[-3:]) if historial else "Ninguno"
    
    prompt_sistema = (
        f"Eres un audiodescriptor profesional experto en sintesis.\n"
        f"MANUAL DE REGLAS:\n{REGLAS_PROFESIONALES}\n"
        f"CONTEXTO DE ESCENA: {instruccion_escena}\n"
    )
    
    prompt_usuario = (
        f"EJEMPLOS:\n{EJEMPLOS_ESTILO}\n"
        f"HISTORIAL RECIENTE: {historial_str}\n"
        f"Genera UNA SOLA oracion corta (max {palabras} palabras) sobre la imagen. "
        f"Aplica las reglas. IMPORTANTE: ¡NO repitas el lugar ni la hora si el historial dice que ya estamos ahí! Termina con punto."
    )
    
    p_sistema_utf8 = prompt_sistema.encode('utf-8').decode('utf-8')
    p_usuario_utf8 = prompt_usuario.encode('utf-8').decode('utf-8')

    max_retries = 6
    base_delay = 3.0
    resultado = "Accion en pantalla."
    tokens_spent = 0
    
    for attempt in range(max_retries):
        try:
            response = openai_client.chat.completions.create(
                model=model_name,
                max_tokens=75,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": p_sistema_utf8},
                    {"role": "user", "content": [
                        {"type": "text", "text": p_usuario_utf8},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]}
                ]
            )
            resultado = response.choices[0].message.content.strip()
            if hasattr(response, 'usage') and response.usage:
                tokens_spent = getattr(response.usage, 'total_tokens', 0)
            if not tokens_spent:
                tokens_spent = len(p_sistema_utf8.split()) + len(p_usuario_utf8.split()) + len(resultado.split()) + 1000
            break
        except Exception as e:
            error_str = str(e).lower()
            if 'rate_limit' in error_str or '429' in error_str or 'too many requests' in error_str:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   [Wait] Rate limit alcanzado. Reintentando en {delay} seg... (Intento {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
            print(f"   [Error] Error IA ({model_name}): {e}")
            # Fallback en caso de que el modelo free falle con visión
            if "vision" in error_str or "image" in error_str or "unsupported" in error_str:
                try:
                    print("   [Fallback] Reintentando sin imagen directa como fallback...")
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
        os.remove(frame_path)
    except:
        pass

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
def procesar_video_task(video_url, api_key, proyecto_id, user_email=None, uid=None, role="free", user_tokens_used=0, user_tokens_total=50000):
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

        # 2. Control de Consumo y Estimación (Free y Pro comparten la misma cuota)
        with VideoFileClip(video_path) as v_clip:
            duracion_segundos = v_clip.duration
        duracion_minutos = duracion_segundos / 60.0
        tokens_estimados = int(duracion_minutos * (DEFAULT_VISION_TOKENS_PER_MIN + DEFAULT_TEXT_TOKENS_PER_MIN))

        # Recuperar estado actualizado de tokens si existe en Redis
        cur_tokens_used = max(0, int(user_tokens_used or 0))
        cur_tokens_total = int(user_tokens_total or 0)
        redis_loaded = False
        if uid:
            try:
                raw_u = redis_client.get(f"user_data:{uid}")
                if raw_u:
                    u_cached = json.loads(raw_u.decode('utf-8'))
                    cur_tokens_used = int(u_cached.get("tokens_used", cur_tokens_used) or 0)
                    cur_tokens_total = int(u_cached.get("tokens_total", cur_tokens_total) or 0)
                    redis_loaded = True

                    # Renovación automática de cuota cada 30 días.
                    now_ts = int(time.time())
                    last_reset = int(u_cached.get("last_reset_at", 0) or 0)
                    if last_reset and (now_ts - last_reset) >= 30 * 86400:
                        cur_tokens_used = 0
                        u_cached["tokens_used"] = 0
                        u_cached["last_reset_at"] = now_ts
                        redis_client.set(f"user_data:{uid}", json.dumps(u_cached), ex=86400 * 30)
                        print(f"[Tokens] Renovación automática de cuota para {user_email}")
            except Exception as e_red:
                print(f"[Redis Cache Warning]: {e_red}")

        # Cuota autoritativa: si Redis no tiene datos del usuario, no se confía
        # en el total enviado por el navegador; se usa el valor del plan.
        if not redis_loaded or cur_tokens_total <= 0:
            if role == "admin":
                cur_tokens_total = 999999999
            elif role == "premium":
                cur_tokens_total = PLAN_PRO_TOKENS
            else:
                cur_tokens_total = PLAN_FREE_TOKENS

        # Bloqueo de API si se agotó la cuota (Free y Pro)
        if role in ("free", "premium") and (cur_tokens_used + tokens_estimados) > cur_tokens_total:
            err_msg = (
                f"Límite de tokens mensual agotado. Has consumido {cur_tokens_used:,} de {cur_tokens_total:,} tokens. "
                f"Este video requiere ~{tokens_estimados:,} tokens. Por favor renueva tu suscripción o adquiere tokens adicionales."
            )
            print(f"[Bloqueo API] {err_msg}")
            guardar_fase1_status(proyecto_id, "quota_exceeded", err_msg)
            return {"status": "quota_exceeded", "message": err_msg}

        # El plan Pro necesita una API key asignada por el administrador.
        if role == "premium" and not api_key:
            err_msg = "Tu plan Pro aún no tiene una API key asignada por el administrador."
            guardar_fase1_status(proyecto_id, "error", err_msg)
            return {"status": "error", "message": err_msg}

        # 3. Inicialización dinámica del cliente de IA (OpenRouter Free vs OpenAI Premium)
        http_client = httpx.Client(trust_env=False)

        if role == "free":
            # Usar OpenRouter con modelos gratuitos de forma transparente
            or_key = OPENROUTER_API_KEY or (api_key if api_key and api_key.startswith("sk-or-") else "free_openrouter_key")
            openai_client = OpenAI(
                base_url=OPENROUTER_BASE_URL,
                api_key=or_key,
                default_headers={
                    "HTTP-Referer": OPENROUTER_REFERER,
                    "X-Title": OPENROUTER_TITLE
                },
                http_client=http_client
            )
            model_to_use = OPENROUTER_FREE_MODEL
            print(f"[OpenRouter Free] Usando modelo {model_to_use} con cabeceras requeridas.")
        else:
            # Rol Premium o Superadmin
            if api_key and api_key.startswith("sk-or-"):
                openai_client = OpenAI(
                    base_url=OPENROUTER_BASE_URL,
                    api_key=api_key,
                    default_headers={
                        "HTTP-Referer": OPENROUTER_REFERER,
                        "X-Title": OPENROUTER_TITLE
                    },
                    http_client=http_client
                )
                model_to_use = "openai/gpt-4o-mini"
            else:
                openai_client = OpenAI(api_key=api_key, http_client=http_client)
                model_to_use = "gpt-4o-mini"

        # 4. Detección de silencios Silero VAD
        huecos = obtener_huecos_silencio(video_path)
        historial_ad = []
        bloques_guion = []
        tokens_reales_acumulados = 0

        for i, (inicio, fin) in enumerate(huecos):
            texto, tokens_escena = analizar_con_openai(openai_client, video_path, inicio, fin, i, historial_ad, model_name=model_to_use)
            tokens_reales_acumulados += tokens_escena
            historial_ad.append(texto)
            if len(historial_ad) > 6: 
                historial_ad.pop(0)
            
            # Pre-generar TTS
            voz_default = "es-ES-AlvaroNeural"
            if texto:
                ruta_cache = obtener_ruta_cache_tts(texto, voz_default)
                generar_tts_robusto(texto, voz_default, ruta_cache)
                
                try:
                    clip_audio = AudioFileClip(ruta_cache)
                    duracion_audio = clip_audio.duration
                    clip_audio.close()
                except Exception as e:
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

        # 5. Acumular consumo de tokens para todos los roles (Free y Pro)
        nuevo_tokens_used = cur_tokens_used + tokens_reales_acumulados
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
                    "last_usage_at": int(time.time())
                })
                redis_client.set(f"user_data:{uid}", json.dumps(u_cached), ex=86400 * 30)
                print(f"[Tokens] Consumo actualizado para {user_email} ({role}): {nuevo_tokens_used:,} tokens (+{tokens_reales_acumulados})")
            except Exception as e_tok:
                print(f"[Redis Warning] {e_tok}")

        # Guardar resultado en Redis
        tokens_restantes = max(0, cur_tokens_total - nuevo_tokens_used)
        tokens_por_minuto = DEFAULT_VISION_TOKENS_PER_MIN + DEFAULT_TEXT_TOKENS_PER_MIN
        res_payload = {
            "bloques": bloques_guion,
            "tokens_consumed": tokens_reales_acumulados,
            "tokens_used_total": nuevo_tokens_used,
            "tokens_total": cur_tokens_total,
            "tokens_remaining": tokens_restantes,
            "minutes_remaining": round(tokens_restantes / float(tokens_por_minuto), 2) if tokens_por_minuto else 0,
            "role": role
        }
        try:
            redis_client.set(f"fase1_resultado:{proyecto_id}", json.dumps(res_payload, ensure_ascii=False), ex=86400)
        except Exception as e_red:
            print(f"[Redis Error]: {e_red}")

        guardar_fase1_status(proyecto_id, "idle")
        print(f"[Celery] Tarea Fase 1 completada con éxito para {proyecto_id} ({len(bloques_guion)} bloques)")
        return {"status": "success", "bloques_count": len(bloques_guion), "tokens_consumed": tokens_reales_acumulados}

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
    """Actualiza rol, tokens y api_key de un usuario con validación estricta de seguridad."""
    data = request.json or {}
    uid = data.get("uid")
    target_email = data.get("target_email", "").strip()
    new_role = data.get("role", "free")
    tokens_total = data.get("tokens_total")
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

    try:
        raw_u = redis_client.get(f"user_data:{uid}")
        u_data = json.loads(raw_u.decode('utf-8')) if raw_u else {}
        u_data.update({
            "uid": uid,
            "email": target_email,
            "role": effective_role,
            "tokens_total": effective_total,
            "api_key": api_key if api_key else u_data.get("api_key", ""),
            "tokens_used": u_data.get("tokens_used", 0),
            "last_reset_at": u_data.get("last_reset_at", int(time.time()))
        })
        redis_client.set(f"user_data:{uid}", json.dumps(u_data), ex=86400 * 30)
    except Exception as e:
        print(f"[Redis Warning] {e}")

    return jsonify({
        "status": "success",
        "message": f"Usuario {target_email} actualizado con rol {effective_role} y cuota de {effective_total:,} tokens."
    }), 200

@app.route('/api/admin/user/reset-tokens', methods=['POST'])
@require_admin
def api_admin_reset_tokens():
    """Reinicia a 0 los tokens consumidos de un usuario para un nuevo ciclo de facturación."""
    data = request.json or {}
    uid = data.get("uid")
    if not uid:
        return jsonify({"status": "error", "message": "Falta parámetro uid"}), 400

    try:
        raw_u = redis_client.get(f"user_data:{uid}")
        if raw_u:
            u_data = json.loads(raw_u.decode('utf-8'))
            u_data["tokens_used"] = 0
            u_data["last_reset_at"] = int(time.time())
            redis_client.set(f"user_data:{uid}", json.dumps(u_data), ex=86400 * 30)
    except Exception as e:
        print(f"[Redis Warning] {e}")

    return jsonify({"status": "success", "message": "Contador de consumo reiniciado a 0 exitosamente."}), 200

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

        if new_openai:
            OPENAI_API_KEY = new_openai
            try:
                redis_client.set("config:openai_api_key", new_openai)
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
    data = request.json or {}
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

    # La clave se resuelve SIEMPRE en el servidor. Se ignora cualquier
    # api_key que venga del navegador para evitar filtraciones.
    api_key = obtener_api_key_servidor(uid, role)

    if role == "premium" and not api_key:
        msg = ("Tu plan Pro aún no tiene una API key asignada. "
               "El administrador debe asignarla desde el panel de administración antes de procesar videos.")
        guardar_fase1_status(proyecto_id, "error", msg)
        return jsonify({"status": "error", "message": msg}), 400

    if role == "admin" and not api_key:
        msg = "El servidor no tiene configurada la OPENAI_API_KEY. Como Superadmin, puedes ingresarla directamente desde el Panel de Administrador (sección Configuración de IA)."
        guardar_fase1_status(proyecto_id, "error", msg)
        return jsonify({"status": "error", "message": msg}), 500

    if role == "free" and not api_key:
        api_key = "free_user"

    print(f"[Info] Encolando Fase 1: proyecto={proyecto_id}, email={user_email}, role={role}")
    guardar_fase1_status(proyecto_id, "processing")
    
    procesar_video_task.delay(video_url, api_key, proyecto_id, user_email, uid, role, tokens_used, tokens_total)
    
    return jsonify({
        "status": "processing",
        "message": "El análisis ha comenzado de fondo.",
        "role": role,
        "proyecto_id": proyecto_id
    }), 202

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
            audio_final = CompositeAudioClip([
                audio_original.volumex(0.6),
                *[a.volumex(1.5) for a in pistas_ad]
            ])
            audio_final.duration = video_original.duration

        video_final = video_original.set_audio(audio_final)
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