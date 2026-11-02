#!/usr/bin/env bash
#
# Smoke test for the Kokoro TTS service.
# Boots the app on an ALTERNATE port (8183) so it won't disturb any live
# instance on the default 8182. Uses disposable audio and owns its listening
# socket; never calls or kills an existing listener. Generates and fetches a WAV.
#
set -euo pipefail

PORT="${SMOKE_PORT:-8183}"
HOST="127.0.0.1"
BASE="http://${HOST}:${PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
PYTHON="${PROJECT_DIR}/venv/bin/python"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/kokoro_smoke.XXXXXX")"
TMP_WAV="${TMP_DIR}/clip.wav"
SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID}" ]]; then
    # The launcher runs one Uvicorn process, without workers or a reloader.
    kill "${SERVER_PID}" 2>/dev/null || true
    # Model inference can delay graceful shutdown; bound cleanup to five seconds.
    for ((attempt = 0; attempt < 50; attempt++)); do
      kill -0 "${SERVER_PID}" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

[[ -x "${PYTHON}" ]] || fail "venv python not found at ${PYTHON}"
[[ "${PORT}" =~ ^[0-9]{1,5}$ ]] && (( 10#${PORT} >= 1 && 10#${PORT} <= 65535 )) \
  || fail "SMOKE_PORT must be between 1 and 65535"
mkdir "${TMP_DIR}/audio"

echo "Starting isolated server on ${BASE} ..."
(
  cd "${PROJECT_DIR}"
  exec "${PYTHON}" - "${PORT}" "${TMP_DIR}/audio" <<'PY'
import socket
import sys
from pathlib import Path

# Reserve the port before importing the app or loading its model. Keeping this
# socket through startup prevents a check-then-bind race with another service.
with socket.socket() as listener:
    listener.bind(("127.0.0.1", int(sys.argv[1])))
    listener.listen()
    Path(sys.argv[2]).parent.joinpath("bound").touch()
    import app
    import uvicorn
    app.AUDIO_DIR = Path(sys.argv[2])
    uvicorn.Server(uvicorn.Config(app.app, log_level="info")).run(sockets=[listener])
PY
) > "${TMP_DIR}/server.log" 2>&1 &
SERVER_PID=$!

# Poll /health until model_loaded=true (model load can take a while).
echo "Waiting for model to load ..."
DEADLINE=$((SECONDS + 120))
LOADED=0
while (( SECONDS < DEADLINE )); do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    cat "${TMP_DIR}/server.log" >&2 || true
    fail "server process died during startup"
  fi
  # Do not probe until our child has claimed the socket, even on startup failure.
  if [[ -f "${TMP_DIR}/bound" ]]; then
    HEALTH="$(curl -s --max-time 3 "${BASE}/health" || true)"
    if echo "${HEALTH}" | grep -q '"model_loaded":true'; then
      LOADED=1
      echo "Model loaded: ${HEALTH}"
      break
    fi
  fi
  sleep 2
done
(( LOADED == 1 )) || fail "model did not load within timeout"

echo "POST /api/generate ..."
GEN="$(curl -s --max-time 60 -X POST "${BASE}/api/generate" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello Sam, this is a smoke test.","voice":"af_bella"}')"
echo "Response: ${GEN}"

AUDIO_URL="$(echo "${GEN}" | "${PYTHON}" -c 'import sys,json; print(json.load(sys.stdin)["audio_url"])')" \
  || fail "no audio_url in generate response"
[[ -n "${AUDIO_URL}" ]] || fail "empty audio_url"

echo "GET ${AUDIO_URL} ..."
HTTP_CODE="$(curl -s --max-time 30 -o "${TMP_WAV}" -w '%{http_code}' "${BASE}${AUDIO_URL}")"
[[ "${HTTP_CODE}" == "200" ]] || fail "audio fetch returned HTTP ${HTTP_CODE}"

SIZE="$(stat -f%z "${TMP_WAV}" 2>/dev/null || stat -c%s "${TMP_WAV}")"
(( SIZE > 1000 )) || fail "downloaded WAV suspiciously small (${SIZE} bytes)"
head -c 4 "${TMP_WAV}" | grep -q "RIFF" || fail "downloaded file is not a RIFF/WAV"

echo "SMOKE PASS: generated and fetched ${SIZE}-byte WAV from ${AUDIO_URL}"
