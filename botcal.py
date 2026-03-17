import os
import telebot
from flask import Flask, request  # Borramos 'app' de la importación
from dotenv import load_dotenv
from groq import Groq
import base64
import threading


ruta_env = os.path.join(os.path.dirname(__file__), "api_cal_ai.env")
load_dotenv(ruta_env)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()


if not BOT_TOKEN:
    raise ValueError("No se encontró BOT_TOKEN en api_cal_ai.env")
if not GROQ_API_KEY:
    raise ValueError("No se encontró GROQ_API_KEY en api_cal_ai.env")

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

SESSION_TIMEOUT_SECONDS = 60 * 60
active_sessions = set()
session_timers = {}
session_lock = threading.Lock()

MAX_TELEGRAM_CHARS = 3900

def _split_text(text, max_len=MAX_TELEGRAM_CHARS):
    text = (text or "").strip()
    if not text:
        return []

    parts = []
    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len

        chunk = text[:cut].strip()
        if chunk:
            parts.append(chunk)
        text = text[cut:].strip()

    if text:
        parts.append(text)

    return parts


def _with_end_option(text):
    text = (text or "").strip()
    end_hint = "Opcion: /end para finalizar la conversacion."
    if "/end" in text:
        return text
    return f"{text}\n\n{end_hint}" if text else end_hint


def reply_with_end_option(message, text):
    full_text = _with_end_option(text)
    parts = _split_text(full_text)
    if not parts:
        return

    bot.reply_to(message, parts[0])
    for part in parts[1:]:
        bot.send_message(message.chat.id, part)


def send_with_end_option(chat_id, text):
    full_text = _with_end_option(text)
    for part in _split_text(full_text):
        bot.send_message(chat_id, part)


def _cancel_timer(chat_id):
    timer = session_timers.pop(chat_id, None)
    if timer:
        timer.cancel()


def _auto_close_session(chat_id):
    with session_lock:
        if chat_id not in active_sessions:
            return
        active_sessions.discard(chat_id)
        session_timers.pop(chat_id, None)

    send_with_end_option(
        chat_id,
        "La conversacion se cerro automaticamente por inactividad (1 hora). Si queres volver, usa /start.",
    )


def _schedule_auto_close(chat_id):
    _cancel_timer(chat_id)
    timer = threading.Timer(SESSION_TIMEOUT_SECONDS, _auto_close_session, args=[chat_id])
    timer.daemon = True
    session_timers[chat_id] = timer
    timer.start()


def start_or_refresh_session(chat_id):
    with session_lock:
        active_sessions.add(chat_id)
        _schedule_auto_close(chat_id)


def end_session(chat_id):
    with session_lock:
        if chat_id not in active_sessions:
            return False
        active_sessions.discard(chat_id)
        _cancel_timer(chat_id)
    return True


def has_active_session(chat_id):
    with session_lock:
        return chat_id in active_sessions


@bot.message_handler(func=lambda message: True)
def respuesta_emergencia(message):
    print("DEBUG: Entró al handler de emergencia")
    bot.reply_to(message, "¡Fabrizio, te estoy escuchando!")

@bot.message_handler(commands=['start', 'hello'])
def send_welcome(message):
    start_or_refresh_session(message.chat.id)
    reply_with_end_option(message, "Mandame una foto de comida y te estimo Kcal + Proteina.")


@bot.message_handler(commands=['end'])
def end_conversation(message):
    if end_session(message.chat.id):
        reply_with_end_option(message, "Conversacion finalizada. Cuando quieras volver, usa /start.")
    else:
        reply_with_end_option(message, "No hay una conversacion activa. Usa /start para comenzar.")


@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    text = (message.text or "").strip().lower()
    if text in ("/start", "/hello", "/end"):
        return

    chat_id = message.chat.id
    if not has_active_session(chat_id):
        reply_with_end_option(message, "No hay una conversacion activa. Usa /start para comenzar.")
        return

    # Renueva la sesion usando el ultimo mensaje recibido como referencia.
    start_or_refresh_session(chat_id)
    reply_with_end_option(message, "Por ahora solo proceso fotos de comida. Enviame una foto para estimar Kcal y proteina.")

def analizar_comida(imagen_bytes, mime_type="image/jpeg"):
    prompt = """Rol: Nutricionista experto en visión volumétrica y porciones de gastronomía argentina.
Usuario: Hombre, 1.90m, 71kg, en superávit calórico. Dieta libre de pescado (rechazo total). Suele usar freidora de aire.

Instrucciones internas (NO imprimas estos pasos):
Paso 0: Analizá si hay comida real. Si es un plato vacío o no hay alimentos, abortá y respondé SOLO: "Error: No detecto comida. Confianza: Nula."
Paso 1: Identificá cada ingrediente y su método de cocción. 
Paso 2: Estimá el peso comparando con un plato estándar (26-28cm).
Paso 3: Calculá macros. Añadí un margen de 15-25% de grasas extra SOLO si la comida se ve frita en aceite profundo; si parece hecha en freidora de aire o al horno, reducí ese margen al mínimo.

Responde SOLO en este formato exacto, sin markdown, máximo 300 caracteres:
Kcal: <rango>
Proteina_g: <rango>
Carbos_g: <rango>
Grasas_g: <rango>
Confianza: baja|media|alta
Nota: <1 frase sobre el volumen detectado y el método de cocción asumido>"""
    
    image_b64 = base64.b64encode(imagen_bytes).decode("utf-8")

    response = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_b64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
        max_tokens=300,
    )

    return (response.choices[0].message.content or "No pude estimar esa imagen.").strip()

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    if not has_active_session(chat_id):
        reply_with_end_option(message, "No hay una conversacion activa. Usa /start para comenzar.")
        return

    start_or_refresh_session(chat_id)

    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        lower_path = (file_info.file_path or "").lower()
        if lower_path.endswith(".png"):
            mime_type = "image/png"
        elif lower_path.endswith(".webp"):
            mime_type = "image/webp"
        else:
            mime_type = "image/jpeg"

        resultado = analizar_comida(downloaded_file, mime_type)
        reply_with_end_option(message, resultado)
    except Exception as e:
        reply_with_end_option(message, f"Error procesando la foto: {e}")

# Esta ruta es la que Telegram va a "golpear"
@app.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    print(f"Webhook payload: {json_string}", flush=True)
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@app.route("/")
def webhook():
    bot.remove_webhook()
    # Aquí pones la URL de tu Web App de PythonAnywhere
    bot.set_webhook(url='https://Dermastroke.pythonanywhere.com/' + BOT_TOKEN)
    return "Webhook seteado con éxito", 200

# Tus funciones de manejo de mensajes (on_message, etc) van acá igual que antes...

