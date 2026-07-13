"""
Rain Alert Voice Bot
---------------------
Polls OpenWeatherMap's One Call API 4.0 minute-by-minute forecast, and when
rain is about to start within RAIN_LOOKAHEAD_MINUTES, generates a short
spoken alert with ElevenLabs and pushes it to the user as a Telegram voice
note. Every substantive result the bot produces — rain incoming, or a clear
sky check-in — is sent to the user's chat as a spoken voice note. Nothing
meaningful is left sitting only in the terminal log.

The user tells the bot whether they're "inside" or "outside" via commands,
and the alert wording changes accordingly:
  - inside + rain coming  -> "grab an umbrella before you head out"
  - outside + rain coming -> "close your windows" / "get inside" / "run"

Commands:
  /start        - register this chat for alerts
  /inside       - tell the bot you're indoors
  /outside      - tell the bot you're outdoors
  /checkrain    - force an immediate check (handy for demos)
  /status       - show current tracked state
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from elevenlabs.client import ElevenLabs

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rain-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENWEATHER_API_KEY = os.environ["OPENWEATHER_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
# Flash/Turbo models cost 0.5 credits/char — half of Multilingual v2 and
# eleven_v3, which are both full-price at 1 credit/char. eleven_v3 also
# requires a paid ElevenLabs plan (no free-tier access). Only switch this to
# "eleven_v3" if you're on a paid plan and want the more expressive voice —
# otherwise Flash keeps your free-tier credits lasting far longer.
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")

elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

DEFAULT_LAT = float(os.environ.get("DEFAULT_LAT", "6.5244"))
DEFAULT_LON = float(os.environ.get("DEFAULT_LON", "3.3792"))
DEFAULT_LOCATION_NAME = os.environ.get("DEFAULT_LOCATION_NAME", "your area")

RAIN_LOOKAHEAD_MINUTES = int(os.environ.get("RAIN_LOOKAHEAD_MINUTES", "15"))
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
RAIN_THRESHOLD_MM_H = float(os.environ.get("RAIN_THRESHOLD_MM_H", "0.2"))

# How often to send a spoken "all clear, no rain coming" check-in to chat.
# Kept separate from POLL_INTERVAL_MINUTES so the bot doesn't send a voice
# note every 5 minutes forever when skies are clear — it still checks the
# weather every POLL_INTERVAL_MINUTES, it just only *speaks up* about "no
# rain" at this coarser interval. Rain alerts always go out immediately
# regardless of this setting.
STATUS_UPDATE_INTERVAL_MINUTES = int(os.environ.get("STATUS_UPDATE_INTERVAL_MINUTES", "30"))

# --- In-memory state (fine for a weekend project; swap for a DB if you extend this) ---
# chat_id -> {"location": (lat, lon, name), "posture": "inside"|"outside",
#             "last_alert_ts": float, "last_status_ts": float}
subscribers = {}


def chat_label(chat_id) -> str:
    """Human-readable identifier for logs and messages — e.g.
    'telegram_user at Ikeja' instead of a bare numeric chat ID."""
    state = subscribers.get(chat_id)
    if state and state.get("location"):
        return f"telegram_user at {state['location'][2]}"
    return "telegram_user (no location yet)"


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
def fetch_minutely_forecast(lat: float, lon: float):
    """Returns up to 60 minute-by-minute precipitation records from One Call
    API 4.0's dedicated 1-minute timeline endpoint. Each record has a unix
    timestamp ('dt') and precipitation in mm/h."""
    url = "https://api.openweathermap.org/data/4.0/onecall/timeline/1min"
    params = {
        "lat": lat,
        "lon": lon,
        "units": "metric",
        "appid": OPENWEATHER_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", [])


def minutes_until_rain(minutely_data, threshold_mm_h: float, lookahead_minutes: int):
    """Scans the minutely forecast and returns how many minutes from now
    rain is expected to start, or None if no rain is expected within the
    lookahead window."""
    now = datetime.now(timezone.utc).timestamp()
    for entry in minutely_data:
        minutes_out = (entry["dt"] - now) / 60
        if minutes_out < 0 or minutes_out > lookahead_minutes:
            continue
        if entry.get("precipitation", 0) >= threshold_mm_h:
            return round(minutes_out)
    return None


# ---------------------------------------------------------------------------
# ElevenLabs voice generation
# ---------------------------------------------------------------------------
def generate_voice_alert(text: str) -> bytes:
    """Sends text to ElevenLabs TTS via the official SDK and returns raw MP3
    bytes. A 402 here means the account is out of credits — check
    elevenlabs.io -> Subscription, it's a billing/quota issue, not a code
    bug, regardless of whether you call the API directly or via this SDK."""
    audio_stream = elevenlabs_client.text_to_speech.convert(
        text=text,
        voice_id=ELEVENLABS_VOICE_ID,
        model_id=ELEVENLABS_MODEL_ID,
        output_format="mp3_44100_128",
    )
    return b"".join(audio_stream)


async def speak_and_send(chat_id, text: str, app, tmp_name: str = "rain_bot_voice.mp3", silent: bool = False):
    """The single path every chat-facing output goes through: generate the
    line with ElevenLabs and deliver it as a Telegram voice note. This is
    used for BOTH rain alerts and routine 'no rain expected' check-ins, so
    nothing meaningful only shows up in the terminal log — the user always
    sees (hears) it in chat.

    silent=True delivers the message without a notification sound/buzz —
    used for routine "all clear" check-ins so they don't interrupt the user
    every polling cycle. Rain alerts always pass silent=False.

    Falls back to a plain text message only if the ElevenLabs call itself
    fails (network/API error) — a safety net so the user isn't left with
    silence, not an alternate voice path.
    """
    label = chat_label(chat_id)
    log.info("Sending to %s: %s", label, text)

    try:
        audio_bytes = generate_voice_alert(text)
    except Exception as e:
        # Broad on purpose: the ElevenLabs SDK raises its own error types
        # (e.g. ApiError for a 402/insufficient_credits), not
        # requests.RequestException, and this is a last-resort safety net so
        # a billing hiccup doesn't leave the user with total silence.
        log.error("ElevenLabs TTS failed for %s: %s", label, e)
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"{text}\n\n(Voice generation failed — sent as text. Check your ElevenLabs account/credits.)",
            disable_notification=silent,
        )
        return

    voice_path = f"/tmp/{tmp_name}"
    with open(voice_path, "wb") as f:
        f.write(audio_bytes)

    with open(voice_path, "rb") as f:
        await app.bot.send_voice(chat_id=chat_id, voice=f, caption=text, disable_notification=silent)


# ---------------------------------------------------------------------------
# Alert message wording — this is the "passion" logic: what to actually DO
# ---------------------------------------------------------------------------
def build_alert_text(minutes_out: int, posture: str, location_name: str) -> str:
    if posture == "outside":
        if minutes_out <= 5:
            action = "Get inside now, or start running for cover."
        else:
            action = "Head inside soon and close up before it hits."
        return f"Heads up. Rain is expected over {location_name} in about {minutes_out} minutes. {action}"
    else:
        return (
            f"Heads up. Rain is expected over {location_name} in about {minutes_out} minutes. "
            f"Close your windows, and grab an umbrella if you're heading out."
        )


# ---------------------------------------------------------------------------
# Core check — runs on a schedule for every subscribed chat
# ---------------------------------------------------------------------------
async def run_rain_check_for_chat(chat_id, app):
    state = subscribers.get(chat_id)
    if not state:
        return

    label = chat_label(chat_id)
    lat, lon, location_name = state["location"]
    try:
        minutely = fetch_minutely_forecast(lat, lon)
    except requests.RequestException as e:
        log.error("Weather fetch failed for %s: %s", label, e)
        await app.bot.send_message(
            chat_id=chat_id,
            text="Couldn't reach the weather service just now — I'll retry on the next check.",
        )
        return

    minutes_out = minutes_until_rain(minutely, RAIN_THRESHOLD_MM_H, RAIN_LOOKAHEAD_MINUTES)

    if minutes_out is None:
        # No rain expected — still speak up, just at a coarser interval so
        # we're not sending a voice note every single poll cycle, and
        # delivered silently so a routine "nothing's happening" update
        # doesn't buzz the phone the way a real alert should.
        last_status = state.get("last_status_ts", 0)
        if time.time() - last_status < STATUS_UPDATE_INTERVAL_MINUTES * 60:
            log.info("No rain expected near %s — status already sent recently, skipping", label)
            return

        text = (
            f"All clear near {location_name}. No rain expected in the next "
            f"{RAIN_LOOKAHEAD_MINUTES} minutes."
        )
        await speak_and_send(chat_id, text, app, tmp_name="rain_status.mp3", silent=True)
        state["last_status_ts"] = time.time()
        return

    # Debounce: don't re-alert for the same rain event every poll cycle.
    last_alert = state.get("last_alert_ts", 0)
    if time.time() - last_alert < RAIN_LOOKAHEAD_MINUTES * 60:
        log.info("Already alerted %s recently, skipping", label)
        return

    # A short, loud, unmistakable ping FIRST — Telegram's push preview for a
    # voice note often just shows "🎤 Voice message" with no caption text, so
    # without this a real rain alert can look identical to routine chatter.
    # This message rings normally (disable_notification=False); the voice
    # note that follows is sent quietly since the user's already been
    # alerted and just needs to tap and listen.
    await app.bot.send_message(
        chat_id=chat_id,
        text=f"🔔🌧️ Rain alert — {minutes_out} min out near {location_name}. Voice note incoming.",
        disable_notification=False,
    )

    text = build_alert_text(minutes_out, state.get("posture", "inside"), location_name)
    await speak_and_send(chat_id, text, app, tmp_name="rain_alert.mp3", silent=True)
    state["last_alert_ts"] = time.time()


def poll_all_subscribers(app):
    """Scheduler entrypoint — fans out to every subscribed chat."""
    for chat_id in list(subscribers.keys()):
        app.create_task(run_rain_check_for_chat(chat_id, app))


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers[chat_id] = {
        # Falls back to the default location until the user shares their own.
        "location": (DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION_NAME),
        "posture": "inside",
        "last_alert_ts": 0,
        "last_status_ts": 0,
    }

    # A one-tap "share my location" button — works identically on mobile and
    # desktop Telegram clients. Desktop users get a location picker; mobile
    # users get their device GPS.
    location_button = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share my location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        f"You're registered for rain alerts (currently defaulting to {DEFAULT_LOCATION_NAME} "
        "until you share your own location).\n\n"
        "Tap the button below to share your location so alerts match exactly where you are.\n"
        "Tell me where you are with /inside or /outside — I'll adjust the advice.\n"
        "Use /checkrain any time to force a check (handy for testing).\n"
        f"I check every {POLL_INTERVAL_MINUTES} minutes and warn you about "
        f"{RAIN_LOOKAHEAD_MINUTES} minutes before rain starts.",
        reply_markup=location_button,
    )


async def receive_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a location shared via the request_location button (or Telegram's
    native attachment -> Location menu). Works the same on mobile and desktop."""
    chat_id = update.effective_chat.id
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    # Reverse-geocode to a human-readable name using OpenWeatherMap's free
    # geocoding endpoint, purely for nicer alert wording.
    location_name = f"{lat:.3f}, {lon:.3f}"
    try:
        geo_resp = requests.get(
            "http://api.openweathermap.org/geo/1.0/reverse",
            params={"lat": lat, "lon": lon, "limit": 1, "appid": OPENWEATHER_API_KEY},
            timeout=5,
        )
        geo_resp.raise_for_status()
        results = geo_resp.json()
        if results:
            location_name = results[0].get("name", location_name)
    except requests.RequestException as e:
        log.warning("Reverse geocoding failed, using raw coordinates: %s", e)

    subscribers.setdefault(chat_id, {"posture": "inside", "last_alert_ts": 0, "last_status_ts": 0})
    subscribers[chat_id]["location"] = (lat, lon, location_name)
    subscribers[chat_id].setdefault("posture", "inside")
    subscribers[chat_id].setdefault("last_alert_ts", 0)
    subscribers[chat_id].setdefault("last_status_ts", 0)

    await update.message.reply_text(
        f"Location set to {location_name}. Alerts will now use this spot.\n"
        "Note: this is a one-time snapshot, not live tracking — resend your "
        "location any time you move somewhere new with /setlocation."
    )


async def set_location_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-shows the location-share button on demand (e.g. after the user moves)."""
    location_button = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share my location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Tap below to update your location.", reply_markup=location_button
    )


async def set_inside(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.setdefault(chat_id, {
        "location": (DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION_NAME),
        "last_alert_ts": 0,
        "last_status_ts": 0,
    })
    subscribers[chat_id]["posture"] = "inside"
    await update.message.reply_text("Got it — marked you as inside.")


async def set_outside(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.setdefault(chat_id, {
        "location": (DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION_NAME),
        "last_alert_ts": 0,
        "last_status_ts": 0,
    })
    subscribers[chat_id]["posture"] = "outside"
    await update.message.reply_text("Got it — marked you as outside.")


async def check_rain_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in subscribers:
        await update.message.reply_text("Send /start first.")
        return
    # Force through both debounces so demos always trigger a spoken result.
    subscribers[chat_id]["last_alert_ts"] = 0
    subscribers[chat_id]["last_status_ts"] = 0
    await update.message.reply_text("Checking now...")
    await run_rain_check_for_chat(chat_id, context.application)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = subscribers.get(chat_id)
    if not state:
        await update.message.reply_text("Not registered yet — send /start.")
        return
    lat, lon, name = state["location"]
    await update.message.reply_text(
        f"Location: {name} ({lat}, {lon})\n"
        f"Posture: {state.get('posture', 'inside')}\n"
        f"Poll interval: every {POLL_INTERVAL_MINUTES} min\n"
        f"Lookahead: {RAIN_LOOKAHEAD_MINUTES} min\n"
        f"Spoken 'all clear' check-ins: every {STATUS_UPDATE_INTERVAL_MINUTES} min"
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlocation", set_location_prompt))
    app.add_handler(MessageHandler(filters.LOCATION, receive_location))
    app.add_handler(CommandHandler("inside", set_inside))
    app.add_handler(CommandHandler("outside", set_outside))
    app.add_handler(CommandHandler("checkrain", check_rain_now))
    app.add_handler(CommandHandler("status", status))

    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_all_subscribers, "interval", minutes=POLL_INTERVAL_MINUTES, args=[app])
    scheduler.start()

    log.info("Rain bot running. Polling every %s minutes.", POLL_INTERVAL_MINUTES)
    app.run_polling()


if __name__ == "__main__":
    main()