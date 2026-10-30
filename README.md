# Velora AI Assistant

Velora is a Python voice assistant with a browser-based control interface. It listens for the “Velora” wake word, handles spoken or typed requests, and uses Gemini for general questions.

## Features

- Speech recognition and text-to-speech
- Gemini responses using the Google GenAI Python SDK
- Weather and news lookup when their API keys are configured
- Music playback using the local `musiclibrary.py` mapping
- Browser interface with microphone controls and a WebSocket connection to the assistant

## Technologies

- Python
- Google GenAI, SpeechRecognition, pyttsx3, Requests, WebSockets, and RapidFuzz
- HTML, CSS, and JavaScript

## Requirements

- Python 3.10 or newer
- A working microphone and audio output
- A Gemini API key
- Optional NewsAPI and OpenWeatherMap API keys for those lookups

## Installation

1. Clone the repository and change to its directory.
2. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

   SpeechRecognition uses PyAudio for microphone input. Install a compatible PyAudio build for your operating system if it is not installed automatically.

4. Copy `.env.example` to `.env` and fill in your API keys. `GEMINI_API_KEY` is required. `NEWS_API_KEY` and `WEATHER_API_KEY` are optional.

## Run

Start the assistant from the project directory:

```powershell
python main.py
```

Then open `frontend/index.html` in a browser. The frontend connects to the assistant over `ws://localhost:8765`; keep the Python process running while using the page. You can also interact through the microphone available to the Python process.

## Project files

- `main.py` — assistant logic, speech handling, API integrations, and WebSocket server
- `musiclibrary.py` — local mapping of supported music names to URLs
- `frontend/index.html` — browser interface
- `frontend/script.js` — frontend interaction script (the current page also contains inline JavaScript)
- `.env.example` — names of environment variables required or optionally supported
