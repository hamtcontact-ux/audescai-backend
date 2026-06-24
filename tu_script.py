# -*- coding: utf-8 -*-
import os
import time
import base64
import json
import asyncio
import subprocess
import sys
import urllib.request
# Forzar actualización de edge-tts en el arranque para evitar error 403 de Microsoft
try:
    print("[Startup] Intentando forzar actualización de edge-tts a >=6.1.12...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "edge-tts>=6.1.12"])
    print("[Startup] Actualización de edge-tts completada exitosamente.")
except Exception as e:
    print(f"[Startup Warning] No se pudo forzar la actualización de edge-tts: {e}")
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from openai import OpenAI
import httpx
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip

# Configuración del entorno
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CARPETA_AUDIOS = os.path.join(BASE_DIR, "TEMP_PROCESAMIENTO")
os.makedirs(CARPETA_AUDIOS, exist_ok=True)
TEMP_DIR = CARPETA_AUDIOS

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



import edge_tts
from silero_vad import load_silero_vad, get_speech_timestamps, read_audio

# Estado de procesamiento de Fase 1 (simplificado)
fase1_status = "idle"

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
    
    audio_temp = os.path.join(TEMP_DIR, f"temp_vad_{int(time.time())}.wav")
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

# --- 2. ANÁLISIS CON OPENAI ---
def analizar_con_openai(openai_client, video_path, inicio, fin, indice_escena, historial):
    duracion_max = fin - inicio
    print(f"   [Info] Analizando escena {indice_escena}: {inicio:.1f}s a {fin:.1f}s")
    
    frame_path = os.path.join(TEMP_DIR, f"frame_desc_{int(time.time())}.jpg")
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

    max_retries = 8
    base_delay = 3.0
    resultado = "Accion en pantalla."
    
    for attempt in range(max_retries):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=70,
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
            break
        except Exception as e:
            error_str = str(e).lower()
            if 'rate_limit' in error_str or '429' in error_str or 'too many requests' in error_str:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   [Wait] Rate limit alcanzado. Reintentando en {delay} seg... (Intento {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
            print(f"   [Error] Error IA: {e}")
            break

    try:
        os.remove(frame_path)
    except:
        pass

    return resultado

# --- UTILERÍA DE CACHÉ DE AUDIO TTS ---
def obtener_ruta_cache_tts(texto, voz):
    import hashlib
    filename = f"tts_{hashlib.md5((texto + voz).encode('utf-8')).hexdigest()}.mp3"
    return os.path.join(CARPETA_AUDIOS, filename)

# --- GENERACIÓN DE AUDIO (EDGE TTS) ---
def generar_tts_robusto(texto, voz, path_salida):
    import asyncio
    import edge_tts
    import subprocess
    import os
    import sys

    # Si por alguna razón el archivo ya existe y tiene contenido, retornamos True
    if os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
        return True, ""

    # Asegurar que el directorio contenedor exista
    os.makedirs(os.path.dirname(path_salida), exist_ok=True)

    async def amain():
        communicate = edge_tts.Communicate(texto, voz)
        await communicate.save(path_salida)

    errores = []

    # Intento 1: Loop de eventos local (ideal para hilos de Flask)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(amain())
        loop.close()
        if os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
            return True, ""
    except Exception as e:
        err_msg = f"new_event_loop: {type(e).__name__}: {str(e)}"
        print(f"[Warning robust-tts] {err_msg}")
        errores.append(err_msg)

    # Intento 2: asyncio.run estándar
    try:
        asyncio.run(amain())
        if os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
            return True, ""
    except Exception as e:
        err_msg = f"asyncio.run: {type(e).__name__}: {str(e)}"
        print(f"[Warning robust-tts] {err_msg}")
        errores.append(err_msg)

    # Intento 3: CLI Subprocess usando python -m edge_tts (para ser totalmente portable en hilos)
    try:
        cmd = [sys.executable, "-m", "edge_tts", "--voice", voz, "--text", texto, "--write-media", path_salida]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and os.path.exists(path_salida) and os.path.getsize(path_salida) > 0:
            return True, ""
        else:
            err_msg = f"CLI (returncode={res.returncode}): stdout={res.stdout.strip()}, stderr={res.stderr.strip()}"
            print(f"[Warning robust-tts] {err_msg}")
            errores.append(err_msg)
    except Exception as e:
        err_msg = f"CLI_exception: {type(e).__name__}: {str(e)}"
        print(f"[Warning robust-tts] {err_msg}")
        errores.append(err_msg)

    return False, " | ".join(errores)

def generar_audio_pro(texto, i, voz_id):
    path_audio = os.path.join(TEMP_DIR, f"ad_{i}_{int(time.time())}.mp3")
    voice = voz_id if voz_id else "es-ES-AlvaroNeural" 
    
    # Optimización: si ya existe en la caché, lo copiamos directamente
    ruta_cache = obtener_ruta_cache_tts(texto, voice)
    if os.path.exists(ruta_cache) and os.path.getsize(ruta_cache) > 0:
        try:
            import shutil
            shutil.copyfile(ruta_cache, path_audio)
            return path_audio
        except Exception as e:
            print(f"[Warning] No se pudo copiar desde caché: {e}")

    exito, _ = generar_tts_robusto(texto, voice, path_audio)
    if exito:
        # Guardamos en la caché también
        try:
            import shutil
            shutil.copyfile(path_audio, ruta_cache)
        except:
            pass
        return path_audio
    else:
        print(f"[Error] Error en TTS Edge al generar audio pro.")
        return None

@app.route('/procesar-fase1', methods=['POST'])
def api_fase1():
    global fase1_status
    data = request.json or {}
    video_url = data.get("video_url")
    api_key = data.get("api_key")
    proyecto_id = data.get("proyecto_id")

    if not video_url or not api_key or not proyecto_id:
        return jsonify({"status": "error", "message": "Faltan parámetros obligatorios: video_url, api_key, proyecto_id"}), 400

    print(f"[Info] Iniciando procesamiento de audiodescripción Fase 1 para proyecto: {proyecto_id}")
    fase1_status = "processing"
    
    # Rutas locales relativas de procesamiento
    video_path = os.path.join(TEMP_DIR, f"{proyecto_id}_original.mp4")
    
    try:
        # Descargar video
        descargar_video(video_url, video_path)
        
        # Inicializar cliente OpenAI dinámicamente con la clave del usuario
        http_client = httpx.Client(trust_env=False)
        openai_client = OpenAI(api_key=api_key, http_client=http_client)

        
        # Ejecutar Silero VAD
        huecos = obtener_huecos_silencio(video_path)
        historial_ad = []
        bloques_guion = []

        for i, (inicio, fin) in enumerate(huecos):
            texto = analizar_con_openai(openai_client, video_path, inicio, fin, i, historial_ad)
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
                    print(f"[Warning] No se pudo obtener la duración de audio real: {e}")
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

        fase1_status = "idle"
        return jsonify({"status": "success", "bloques": bloques_guion}), 200

    except Exception as e:
        fase1_status = "idle"
        print(f"[Error] Error en Fase 1 para {proyecto_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/fase1-status', methods=['GET'])
def api_fase1_status():
    global fase1_status
    return jsonify({"status": fase1_status}), 200

@app.route('/fase1-start', methods=['POST'])
def api_fase1_start():
    global fase1_status
    fase1_status = "processing"
    return jsonify({"status": "success"}), 200

@app.route('/api/tts', methods=['GET'])
def api_tts():
    texto = request.args.get("text", "").strip()
    voz = request.args.get("voice", "es-ES-AlvaroNeural")
    if not texto:
        return "Falta texto para sintetizar", 400
    
    path_audio = obtener_ruta_cache_tts(texto, voz)
    
    if os.path.exists(path_audio) and os.path.getsize(path_audio) == 0:
        try:
            os.remove(path_audio)
        except:
            pass

    if not os.path.exists(path_audio) or os.path.getsize(path_audio) == 0:
        exito, errores = generar_tts_robusto(texto, voz, path_audio)
        if not exito:
            return f"Error al generar síntesis de voz edge-tts: {errores}", 500
            
    return send_file(path_audio, mimetype="audio/mpeg", conditional=True)

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
        # Si el video original no está en cache local temporal, lo descargamos
        if not os.path.exists(video_path):
            descargar_video(video_url, video_path)

        video_original = VideoFileClip(video_path)
        audio_original = video_original.audio
        pistas_ad = []
        ruta_mezcla_mp3 = os.path.join(TEMP_DIR, f"{proyecto_id}_mezcla_ad_final.mp3")

        for i, bloque in enumerate(bloques_editados):
            texto = bloque.get("text", "").strip()
            inicio_clip = float(bloque.get("start", 0))
            voz_id = bloque.get("voice", "es-ES-AlvaroNeural")

            if not texto:
                continue

            path_audio = generar_audio_pro(texto, i, voz_id)
            if not path_audio:
                continue

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
    # Esto obliga a Flask a escuchar al proxy de Coolify en el puerto 5000
    app.run(host='0.0.0.0', port=5000, debug=False)