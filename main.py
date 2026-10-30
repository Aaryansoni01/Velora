import logging
import os
import platform
import re
import string
import threading
import time
import asyncio
import websockets
import threading
import requests
import speech_recognition as sr
import webbrowser
import pyttsx3
from dotenv import load_dotenv
from google import genai

import musiclibrary



async def handle_client(websocket):
    print("✅ Frontend Connected")

    try:
        async for message in websocket:
            print("📩 Received from Frontend:", message)

            # Temporary reply
            response = f"Velora: You said '{message}'"

            await websocket.send(response)

    except Exception as e:
        print("WebSocket Error:", e)
        async def websocket_server():
            async with websockets.serve(handle_client, "localhost", 8765):
                print("🚀 WebSocket Server Running")
        await asyncio.Future()
        threading.Thread(
    target=lambda: asyncio.run(websocket_server()),
    daemon=True
).start()

# rapidfuzz gives much better fuzzy-match quality than difflib, but the
# assistant should still run (with slightly weaker matching) if it isn't
# installed. Run: pip install rapidfuzz --break-system-packages
try:
    from rapidfuzz import fuzz as _rf_fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    import difflib
    _HAS_RAPIDFUZZ = False

# ---------------------------------------------------------------------------
# Setup & configuration
# ---------------------------------------------------------------------------

load_dotenv()  # reads GEMINI_API_KEY, NEWS_API_KEY, WEATHER_API_KEY from .env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("velora")

# ---------------------------------------------------------------------------
# Wake word configuration
# ---------------------------------------------------------------------------
# Google's speech recognizer has no language-model entry for "Velora" - it's
# not a real English word - so it always substitutes the closest sounding
# real words it knows ("every set", "ever set", "everset", ...). An exact
# substring check like `"velora" in text` can therefore never match, no
# matter how clearly the word is spoken. Fuzzy matching against a list of
# known mis-hearings/variants is what actually fixes this.
WAKE_WORD_VARIANTS = [
    "velora",
    "valora",
    "vaalora",
    "velorah",
    "vailora",
    "veloraa",
    "hey velora",
    "hey valora",
    # Real mishearings you reported for "Velora" on your mic/accent. Plain
    # character-similarity scoring alone does NOT catch these reliably -
    # "every set" and "velora" only score ~55-60% against each other even
    # though they sound alike, because Google's STT already threw away the
    # phonetic signal and forced the audio onto real English words it
    # knows. Listing the exact known mishearings as variants sidesteps
    # that: run the assistant, watch the [WAKE] debug log below for what
    # your voice actually transcribes to, and add any new patterns here.
    "every set",
    "ever set",
    "everset",
    "eleora",
    "elora",
    "avelora",
]

# Similarity score (0-100) a transcript needs to reach against the closest
# variant before we treat it as the wake word. 70-80 is the sweet spot:
# high enough to reject unrelated speech, low enough to absorb STT noise.
WAKE_WORD_THRESHOLD = 72

WAKE_WORD = "velora"  # kept for readability/back-compat in comments & prompts

# Phrases that send the assistant back to wake-word mode while in active
# listening mode. Checked as substrings so minor STT variations still match.
EXIT_PHRASES = ("sleep", "stop listening", "goodbye")

# How long (seconds) the assistant stays in active listening mode without
# hearing anything before it automatically falls back to wake-word mode.
ACTIVE_SILENCE_TIMEOUT = 45

# Each listen() call in active mode is capped at this many seconds so the
# remaining silence budget is rechecked frequently instead of blocking in
# one long call that could overshoot the 45s window.
ACTIVE_LISTEN_CHUNK = 8

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found. Check your .env file.")

client = genai.Client(api_key=GEMINI_API_KEY)

recognizer = sr.Recognizer()
recognizer.pause_threshold = 0.8
recognizer.dynamic_energy_threshold = True
# A sane starting point before adjust_for_ambient_noise() recalibrates it in
# run_assistant(). Leaving this at the sr default (300) is fine for most
# rooms; dynamic_energy_threshold=True lets it drift with real conditions.
recognizer.energy_threshold = 300

# pyttsx3 is NOT thread-safe: it caches engine/driver instances internally
# (a module-level weakref dict keyed only by driver name), so two threads
# calling pyttsx3.init() close together can end up sharing the SAME engine
# object. Calling runAndWait() on that shared engine from two threads at
# once deadlocks (one or both calls never return) or silently corrupts the
# engine's state. This lock forces every speak() call - blocking or not -
# to fully finish before the next one may start, which is what prevents
# the assistant from hanging after the wake word / after a command.
_tts_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------

def speak(text, blocking=True):
    """Speak text out loud.

    Uses the platform's default TTS driver instead of hardcoding Windows'
    sapi5, so this also works on macOS/Linux.

    A new pyttsx3 engine is created per call (rather than reused) because
    pyttsx3 engines are known to misbehave when runAndWait() is called
    repeatedly on the same instance. All calls (blocking or not) serialize
    on _tts_lock so no two engines ever run concurrently - see comment
    above. blocking=False still returns to the caller immediately so the
    microphone can keep listening while speech plays; only the background
    thread itself waits on the lock if one is needed.
    """

    def run():
        log.info(f"[TTS] Acquiring speech lock to say: '{text}'")
        with _tts_lock:
            try:
                engine = pyttsx3.init('sapi5') if platform.system() == "Windows" else pyttsx3.init()
                engine.setProperty('rate', 175)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
                log.info("[TTS] Finished speaking")
            except Exception as e:
                log.error(f"[TTS] error: {e}")

    if blocking:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Transcript normalization
# ---------------------------------------------------------------------------

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize_text(text):
    """Lowercase, strip punctuation, and collapse whitespace.

    Applied to every transcript before wake-word matching or command
    routing so that things like "Velora," / "velora." / extra double
    spaces from the STT engine don't cause avoidable mismatches.
    """
    if not text:
        return ""
    text = text.lower().strip()
    text = text.translate(_PUNCT_TABLE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Wake word detection (fuzzy)
# ---------------------------------------------------------------------------

def _similarity(a, b):
    """Return a 0-100 similarity score between two short strings."""
    if _HAS_RAPIDFUZZ:
        return _rf_fuzz.ratio(a, b)
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


def detect_wake_word(raw_text):
    """Check whether a transcript is (probably) someone saying the wake word.

    Instead of an exact substring check, this compares every word AND the
    full normalized phrase against a list of known "Velora" mishearings
    (velora, valora, vaalora, ...) using fuzzy string similarity. This is
    what lets STT garbage like "every set" or "ever set" still register as
    a match, since both score high similarity against "velora"/"valora".

    Returns a dict with everything needed for the debug log line, plus
    'detected': bool.
    """
    normalized = normalize_text(raw_text)
    if not normalized:
        return {
            "raw": raw_text, "normalized": normalized,
            "best_variant": None, "score": 0, "detected": False,
        }

    words = normalized.split()
    # Compare individual words, adjacent word-pairs ("hey velora" style),
    # and the whole phrase - covers wake words that get merged/split by STT.
    candidates = set(words)
    candidates.add(normalized)
    for i in range(len(words) - 1):
        candidates.add(f"{words[i]} {words[i + 1]}")

    best_variant, best_score = None, 0
    for candidate in candidates:
        for variant in WAKE_WORD_VARIANTS:
            score = _similarity(candidate, variant)
            if score > best_score:
                best_score, best_variant = score, variant

    detected = best_score >= WAKE_WORD_THRESHOLD

    log.info(f"[WAKE] Raw Transcript : {raw_text}")
    log.info(f"[WAKE] Normalized     : {normalized}")
    log.info(f"[WAKE] Best Match     : {best_variant}")
    log.info(f"[WAKE] Similarity     : {best_score:.0f}%")
    log.info(f"[WAKE] Wake Word Detected: {'YES' if detected else 'NO'}")

    return {
        "raw": raw_text, "normalized": normalized,
        "best_variant": best_variant, "score": best_score,
        "detected": detected,
    }


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

def get_weather(city="Jaipur"):
    """Fetch weather for a city and speak a short report. Prints full details too."""
    if not WEATHER_API_KEY:
        speak("Sorry Boss, weather API key is not set")
        log.warning("WEATHER_API_KEY missing from .env")
        return

    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric"

    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()

        if resp.status_code == 200:
            city_name = data["name"]
            country = data["sys"]["country"]
            temperature = data["main"]["temp"]
            feels_like = data["main"]["feels_like"]
            humidity = data["main"]["humidity"]
            pressure = data["main"]["pressure"]
            weather_desc = data["weather"][0]["description"]
            wind_speed = data["wind"]["speed"]

            print("\n========== WEATHER REPORT ==========")
            print(f"City        : {city_name}, {country}")
            print(f"Temperature : {temperature} \u00b0C")
            print(f"Feels Like  : {feels_like} \u00b0C")
            print(f"Humidity    : {humidity}%")
            print(f"Pressure    : {pressure} hPa")
            print(f"Weather     : {weather_desc}")
            print(f"Wind Speed  : {wind_speed} m/s")
            print("====================================")

            speak(
                f"The weather in {city_name} is {weather_desc} "
                f"with a temperature of {round(temperature)} degrees Celsius, "
                f"feels like {round(feels_like)} degrees, "
                f"and humidity at {humidity} percent."
            )
        else:
            error_msg = data.get("message", "Unknown error")
            log.error(f"Weather API error: {error_msg}")
            speak(f"Sorry Boss, I couldn't find weather for {city}")

    except requests.RequestException as e:
        log.error(f"Weather request failed: {e}")
        speak("Sorry Boss, something went wrong fetching the weather")
    except (KeyError, ValueError) as e:
        log.error(f"Unexpected weather response format: {e}")
        speak("Sorry Boss, something went wrong fetching the weather")


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def _handle_news():
    if not NEWS_API_KEY:
        speak("Sorry Boss, news API key is not set")
        log.warning("NEWS_API_KEY missing from .env")
        return

    try:
        response = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={"country": "us", "apiKey": NEWS_API_KEY},
            timeout=10,
        )
        if response.status_code == 200:
            articles = response.json().get("articles", [])
            speak(f"Here are the top {min(5, len(articles))} headlines, Boss")
            for article in articles[:5]:
                speak(article["title"])
        else:
            log.error(f"News API error: {response.status_code}")
            speak("Sorry Boss, I couldn't fetch the news right now")
    except requests.RequestException as e:
        log.error(f"News fetch error: {e}")
        speak("Sorry Boss, something went wrong fetching the news")


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

def _handle_play(command_text):
    song = command_text.split("play", 1)[1].strip()
    log.info(f"Looking for song: '{song}'")

    if not song:
        speak("Sorry Boss, tell me the name of the song")
        return

    if song in musiclibrary.music:
        webbrowser.open(musiclibrary.music[song])
        speak(f"Playing {song}")
        return

    spoken_words = song.split()
    for key in musiclibrary.music:
        if any(word in key for word in spoken_words):
            webbrowser.open(musiclibrary.music[key])
            speak(f"Playing {key}")
            return

    speak("Sorry Boss, I couldn't find that song")
    log.info(f"Song '{song}' not found in library")


# ---------------------------------------------------------------------------
# Gemini fallback (general questions)
# ---------------------------------------------------------------------------

def _ask_gemini(command_text):
    log.info("[STATE] Sending command to Gemini")
    try:
        gemini_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=command_text,
        )
        answer = gemini_response.text
        log.info("[STATE] Gemini response received")
        log.info(f"Gemini answered: {answer}")
        log.info("[STATE] Speaking response")
        speak(answer)
    except Exception as e:
        log.error(f"Gemini error: {e}")
        speak("Sorry Boss, I couldn't reach Gemini right now")


# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------

def process_command(command_text):
    command_text = normalize_text(command_text)
    log.info(f"[STATE] Command = {command_text}")

    site_map = {
        "open google": "https://google.com",
        "open facebook": "https://facebook.com",
        "open youtube": "https://youtube.com",
        "open linkedin": "https://linkedin.com",
    }
    for phrase, url in site_map.items():
        if phrase in command_text:
            webbrowser.open(url)
            speak(f"Opening {phrase.split(' ', 1)[1].title()}")
            return

    if "news" in command_text:
        _handle_news()
        return

    if "weather" in command_text:
        city = "Jaipur"
        for keyword in ["weather in ", "weather of ", "weather for "]:
            if keyword in command_text:
                city = command_text.split(keyword, 1)[1].strip()
                break
        get_weather(city)
        return

    if command_text.startswith("play"):
        _handle_play(command_text)
        return

    _ask_gemini(command_text)


# ---------------------------------------------------------------------------
# Listening
# ---------------------------------------------------------------------------

def capture_speech(source, timeout, phrase_time_limit):
    """Listen once on an already-open microphone source and try to recognize it.

    Returns a (status, text) tuple:
      status = "ok"             -> text is the recognized string
      status = "timeout"        -> no phrase started within `timeout` (silence)
      status = "unknown"        -> audio was captured but could not be understood
      status = "request_error"  -> the recognition service call failed
      status = "error"          -> any other unexpected failure

    Splitting the "listening" and "recognizing" steps into their own
    try/except blocks (instead of one big try/except) means we always know
    exactly which phase failed, which is what the log lines below make
    visible - if execution ever stalls, the last log line printed tells you
    precisely which phase it stalled in.
    """
    try:
        audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
    except sr.WaitTimeoutError:
        return "timeout", None
    except Exception as e:
        log.error(f"[MIC] Unexpected error while listening: {e}")
        return "error", None

    log.info("[STATE] Audio captured, sending to speech recognizer")
    try:
        # language pinned explicitly (en-IN reads Indian-accented "Velora"
        # attempts more consistently than the library default of en-US;
        # change to "en-US" if you find the opposite in practice).
        text = recognizer.recognize_google(audio, language="en-IN")
        log.info("[STATE] Speech recognized")
        return "ok", text
    except sr.UnknownValueError:
        return "unknown", None
    except sr.RequestError as e:
        log.error(f"[STT] Speech recognition request error: {e}")
        return "request_error", None
    except Exception as e:
        log.error(f"[STT] Unexpected recognition error: {e}")
        return "error", None


def run_assistant():
    speak("Initializing Velora...")

    try:
        with sr.Microphone() as source:
            log.info("Calibrating microphone for ambient noise (one-time)...")
            # 1.5s (up from 1s) gives dynamic_energy_threshold a slightly
            # better baseline read of room noise before we start listening
            # for real, which reduces both missed wake words and false
            # "unknown" results from clipped audio.
            recognizer.adjust_for_ambient_noise(source, duration=1.5)
            log.info(f"[MIC] energy_threshold calibrated to {recognizer.energy_threshold:.0f}")
            log.info("Ready. Listening for wake word 'Hey Velora'...")

            active_mode = False
            last_active_time = 0.0  # timestamp of the last heard/understood speech in active mode

            while True:
                try:
                    # -----------------------------------------------------
                    # ACTIVE MODE: no wake word needed, keep taking commands
                    # -----------------------------------------------------
                    if active_mode:
                        remaining = ACTIVE_SILENCE_TIMEOUT - (time.monotonic() - last_active_time)
                        if remaining <= 0:
                            log.info("[STATE] 45s of silence reached - returning to wake-word mode")
                            active_mode = False
                            continue

                        listen_timeout = min(remaining, ACTIVE_LISTEN_CHUNK)
                        status, text = capture_speech(source, timeout=listen_timeout, phrase_time_limit=7)

                        if status == "ok":
                            log.info(f"You said: {text}")
                            last_active_time = time.monotonic()
                            lowered = normalize_text(text)

                            if any(phrase in lowered for phrase in EXIT_PHRASES):
                                log.info("[STATE] Exit phrase detected - returning to wake-word mode")
                                speak("Going to sleep, Boss")
                                active_mode = False
                                continue

                            process_command(text)
                            last_active_time = time.monotonic()  # account for time spent processing/speaking

                        elif status == "unknown":
                            # Sound was heard but not understood - not silence, so keep the window open.
                            last_active_time = time.monotonic()
                            log.warning("[STATE] Audio captured but could not be understood")

                        elif status == "timeout":
                            # Genuine silence for this chunk; loop back and recheck the overall budget.
                            pass

                        elif status in ("request_error", "error"):
                            time.sleep(1)  # brief backoff before retrying

                        continue

                    # -----------------------------------------------------
                    # WAKE-WORD MODE
                    # -----------------------------------------------------
                    # phrase_time_limit bumped 3 -> 4s so "Hey Velora" has
                    # enough room to be captured in full instead of getting
                    # clipped mid-word, which was itself producing some of
                    # the garbled transcripts.
                    status, text = capture_speech(source, timeout=5, phrase_time_limit=4)

                    if status == "ok":
                        log.info(f"Heard: {text}")
                        result = detect_wake_word(text)
                        if result["detected"]:
                            log.info("[STATE] Wake word detected")
                            log.info("[STATE] Entering active listening mode")
                            # Fire-and-forget acknowledgment: the mic can start listening
                            # for the command immediately instead of waiting for "Yes
                            # Boss!" to finish playing. Safe because speak() serializes
                            # on _tts_lock - see speak()'s docstring.
                            speak("Yes Boss!", blocking=False)
                            active_mode = True
                            last_active_time = time.monotonic()

                    elif status in ("request_error", "error"):
                        log.error("[STATE] Speech recognition service error")
                        time.sleep(1)  # brief backoff before retrying on service errors

                    # "timeout" and "unknown" need no handling here - just keep listening.

                except Exception as e:
                    # Safety net: never let an unexpected error kill the whole
                    # assistant or leave it silently stuck - log it and keep going.
                    log.error(f"[LOOP] Unexpected error in main loop: {e}")
                    time.sleep(1)

    except OSError as e:
        log.error(f"Microphone error: {e}")
        speak("Sorry Boss, I can't access the microphone")
    except KeyboardInterrupt:
        log.info("Shutting down Velora. Goodbye Boss!")


if __name__ == "__main__":
    if not _HAS_RAPIDFUZZ:
        log.warning(
            "[SETUP] rapidfuzz not installed - falling back to difflib for "
            "wake-word matching (works, but rapidfuzz is more accurate). "
            "Install with: pip install rapidfuzz --break-system-packages"
        )

    # Quick sanity check that the Gemini key works. Wrapped in try/except so a
    # flaky network or transient API error doesn't crash the assistant before
    # it even starts listening. Only runs when executed directly, not on import.
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents="Hello")
        log.info(f"Gemini sanity check OK: {response.text}")
    except Exception as e:
        log.warning(f"Gemini sanity check failed ({e}). Continuing anyway.")

    run_assistant()