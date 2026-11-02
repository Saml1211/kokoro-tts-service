# Kokoro TTS Service

A small FastAPI wrapper around [`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx)
that runs the **Kokoro-82M** text-to-speech model locally and exposes a simple HTTP API.
Fully offline once the model files are downloaded; no API keys, no cloud calls.

- Synthesises text to a WAV file and returns a URL to fetch it.
- 13 built-in voices (US/UK, male/female).
- Adjustable speech speed.
- Auto-cleans the `audio/` directory (keeps the most recent 100 clips).

## Requirements

- Python 3.13 (a `venv/` is already set up in this directory).
- The two Kokoro model files (see **Model download** below). These are git-ignored
  because they are large (~325 MB + ~6 MB).

## Setup

If you need to recreate the virtualenv from scratch:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Model download (required)

The service expects these two files in `models/`:

| File                  | Size    | Source |
|-----------------------|---------|--------|
| `kokoro-v0_19.onnx`   | ~325 MB | https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx |
| `voices.bin`          | ~6 MB   | https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin |

Download them into `models/`:

```bash
mkdir -p models
curl -L -o models/kokoro-v0_19.onnx \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx
curl -L -o models/voices.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin
```

(Releases page: https://github.com/thewh1teagle/kokoro-onnx/releases — look for the
`model-files` tag if the direct links ever move.)

## Run

```bash
./venv/bin/python app.py
```

The server listens on `0.0.0.0:8182` by default. Override with env vars:

```bash
KOKORO_HOST=127.0.0.1 KOKORO_PORT=8183 ./venv/bin/python app.py
```

## Voices

| Voice          | Accent / gender   |
|----------------|-------------------|
| `af_bella`     | American female   |
| `af_nicole`    | American female   |
| `af_sky`       | American female   |
| `af_sarah`     | American female   |
| `af_heart`     | American female   |
| `am_adam`      | American male     |
| `am_michael`   | American male     |
| `bf_emma`      | British female    |
| `bf_isabella`  | British female    |
| `bf_isabelle`  | British female    |
| `bm_george`    | British male      |
| `bm_lewis`     | British male      |
| `bm_daniel`    | British male      |

Partial names also work (e.g. `"bella"` resolves to `af_bella` when unambiguous).

## API

| Method | Path                | Purpose                              |
|--------|---------------------|--------------------------------------|
| GET    | `/`                 | Service info + voice list            |
| GET    | `/health`           | Health check (`model_loaded` flag)   |
| GET    | `/api/voices`       | List supported voices                |
| POST   | `/api/generate`     | Synthesise text -> returns audio URL |
| GET    | `/audio/{filename}` | Fetch a generated WAV                |

### Example

```bash
# Generate
curl -s -X POST http://127.0.0.1:8182/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello Sam, this is Kokoro.","voice":"af_bella","speed":1.0}'
# -> {"audio_url":"/audio/kokoro_af_bella_1234567890.wav", ...}

# Fetch the WAV (use the audio_url from the response above)
curl -s -o out.wav http://127.0.0.1:8182/audio/kokoro_af_bella_1234567890.wav
```

`format` accepts only `"wav"`; any other value is rejected (MP3 is not produced).

## Smoke test

Boots a separate loopback server on port 8183, waits for the existing local
model, generates one clip, fetches it, and cleans up. Audio, download and logs
use a private temporary directory: the normal `audio/` retention policy cannot
remove live clips. The script reserves its socket before model startup, refuses
an occupied port without making HTTP requests to it, and only terminates its own
child process. It never restarts the live service.

```bash
./scripts/smoke_test.sh
# Optional alternate test port (1–65535):
SMOKE_PORT=8184 ./scripts/smoke_test.sh
```

The smoke run still loads a separate model and uses CPU/memory; it is not a
live-service latency or listening-quality check. Exit 0 means a WAV was generated
and fetched; startup, generation and fetch failures return nonzero. Temporary
files are removed on normal exit and INT/TERM (not SIGKILL or power loss).
Cleanup allows its own child five seconds to stop gracefully, then kills that
child if model inference is blocking shutdown.

Regression checks run the same entry point in disposable repository copies,
keeping real FastAPI/Uvicorn and WAV handling but substituting synthesis. They
cover success, occupied-port ownership, invalid ports, model startup failure and
TERM during a blocking model load:

```bash
./venv/bin/python -B tests/test_smoke_isolation.py
# Existing retention check (no model load):
./venv/bin/python -B -c 'from tests.test_cleanup import test_cleanup_keeps_newest_n; test_cleanup_keeps_newest_n()'
```

## Auto-start on macOS (launchd)

A sample agent is provided at `launchd/com.samlyndon.kokoro-tts.plist`.
To enable auto-start at login:

```bash
cp launchd/com.samlyndon.kokoro-tts.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.samlyndon.kokoro-tts.plist
```

To stop / disable:

```bash
launchctl unload ~/Library/LaunchAgents/com.samlyndon.kokoro-tts.plist
```

Logs go to `logs/kokoro-tts.log` and `logs/kokoro-tts-error.log`.
