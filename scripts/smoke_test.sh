#!/usr/bin/env bash
#
# Smoke test for the Kokoro TTS service.
# Boots the app on an ALTERNATE port (8183) so it won't disturb any live
# instance on the default 8182, waits for the model to load, generates one
# clip, fetches the WAV, and exits non-zero on any failure.
#
set -euo pipefail

PORT="${SMOKE_PORT:-8183}"
HOST="127.0.0.1"
BASE="http://${HOST}:${PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
PYTHON="${PROJECT_DIR}/venv/bin/python"
TMP_WAV="$(mktemp /tmp/kokoro_smoke.XXXXXX.wav)"
SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID}" ]]; then
    # Kill the whole process group (uvicorn may have child processes).
    kill -- -"${SERVER_PID}" 2>/dev/null || kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  # Belt and braces: kill anything still listening on our test port.
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:${PORT}" 2>/dev/null | xargs -r kill 2>/dev/null || true
  fi
  rm -f "${TMP_WAV}"
}
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

[[ -x "${PYTHON}" ]] || fail "venv python not found at ${PYTHON}"

echo "Starting server on ${BASE} ..."
# Run in its own process group (set -m) so cleanup can kill the whole group.
set -m
( cd "${PROJECT_DIR}" && exec env KOKORO_HOST="${HOST}" KOKORO_PORT="${PORT}" "${PYTHON}" app.py ) \
  > /tmp/kokoro_smoke_server.log 2>&1 &
SERVER_PID=$!
set +m

# Poll /health until model_loaded=true (model load can take a while).
echo "Waiting for model to load ..."
DEADLINE=$((SECONDS + 120))
LOADED=0
while (( SECONDS < DEADLINE )); do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    cat /tmp/kokoro_smoke_server.log >&2 || true
    fail "server process died during startup"
  fi
  HEALTH="$(curl -s --max-time 3 "${BASE}/health" || true)"
  if echo "${HEALTH}" | grep -q '"model_loaded":true'; then
    LOADED=1
    echo "Model loaded: ${HEALTH}"
    break
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
