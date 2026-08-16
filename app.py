"""
Kokoro-82M TTS Service
FastAPI wrapper around kokoro-onnx for high-quality text-to-speech.
Port: 8182 | Endpoint: POST /api/generate
"""

import os
import time
import logging
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from kokoro_onnx import Kokoro

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUDIO_DIR = Path(__file__).parent / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("KOKORO_HOST", "0.0.0.0")
PORT = int(os.environ.get("KOKORO_PORT", "8182"))

# Supported voices (Kokoro-82M v0.19+ voice set)
SUPPORTED_VOICES = [
    # American Female
    "af_bella", "af_nicole", "af_sky", "af_sarah", "af_heart",
    # American Male
    "am_adam", "am_michael",
    # British Female
    "bf_emma", "bf_isabella", "bf_isabelle",
    # British Male
    "bm_george", "bm_lewis", "bm_daniel",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kokoro-tts")

# ---------------------------------------------------------------------------
# Model singleton
# ---------------------------------------------------------------------------
kokoro_instance: Kokoro | None = None
model_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load Kokoro model on startup."""
    global kokoro_instance
    logger.info("Loading Kokoro-82M model …")
    t0 = time.time()
    models_dir = Path(__file__).parent / "models"
    kokoro_instance = Kokoro(str(models_dir / "kokoro-v0_19.onnx"), str(models_dir / "voices.bin"))
    logger.info(f"Model loaded in {time.time() - t0:.2f}s")
    # Cleanup old audio files (keep last 100)
    files = sorted(AUDIO_DIR.glob("*.wav"), key=lambda f: f.stat().st_mtime)
    if len(files) > 100:
        for f in files[:-100]:
            f.unlink()
            logger.info(f"Cleaned up old audio: {f.name}")
    yield
    logger.info("Shutting down Kokoro TTS service.")


app = FastAPI(title="Kokoro TTS Service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000, description="Text to synthesise")
    voice: str = Field("af_bella", description="Voice identifier")
    speed: float = Field(1.0, ge=0.5, le=2.0, description="Speech speed multiplier")
    # Only WAV is produced today. Field kept for API stability; non-wav is rejected
    # rather than silently ignored, so callers don't expect an MP3 they won't get.
    format: str = Field("wav", pattern="^wav$", description="Output format (only 'wav' supported)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "service": "Kokoro TTS",
        "version": "1.0.0",
        "model": "kokoro-v0_19 (82M)",
        "endpoint": "POST /api/generate",
        "voices": SUPPORTED_VOICES,
    }


@app.get("/api/voices")
async def list_voices():
    return {"voices": SUPPORTED_VOICES}


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    global kokoro_instance
    if kokoro_instance is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    voice = req.voice
    # Allow partial voice names (e.g. "bella" → "af_bella")
    if voice not in SUPPORTED_VOICES:
        matches = [v for v in SUPPORTED_VOICES if voice in v]
        if len(matches) == 1:
            voice = matches[0]
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown voice '{req.voice}'. Available: {SUPPORTED_VOICES}",
            )

    logger.info(f"Generating: voice={voice}, speed={req.speed}, text_len={len(req.text)}")
    t0 = time.time()

    try:
        async with model_lock:
            samples, sample_rate = await asyncio.to_thread(
                kokoro_instance.create, req.text, voice=voice, speed=req.speed
            )
    except Exception as e:
        logger.error(f"Generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")

    # Write audio file
    ts = int(time.time() * 1000)
    filename = f"kokoro_{voice}_{ts}.wav"
    filepath = AUDIO_DIR / filename
    sf.write(str(filepath), samples, sample_rate)

    elapsed = time.time() - t0
    logger.info(f"Generated {filename} in {elapsed:.2f}s ({len(samples)/sample_rate:.1f}s audio)")

    return JSONResponse({
        "audio_url": f"/audio/{filename}",
        "filename": filename,
        "voice": voice,
        "speed": req.speed,
        "duration_s": round(len(samples) / sample_rate, 2),
        "generation_time_s": round(elapsed, 2),
        "sample_rate": sample_rate,
    })


@app.post("/api/generate/raw")
async def generate_raw(req: GenerateRequest):
    """Synthesise and return PCM16 on the response body.

    /api/generate writes a WAV to disk and returns a URL, so a caller that
    wants audio pays for a disk write plus a second HTTP round trip before the
    first sample plays. For a live voice loop that cost is on the critical
    path. This endpoint keeps the samples in memory and hands them straight
    back.

    Returns little-endian signed 16-bit mono PCM. Sample rate is on the
    X-Sample-Rate header, because raw PCM carries no container to hold it.
    """
    global kokoro_instance
    if kokoro_instance is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    voice = req.voice
    if voice not in SUPPORTED_VOICES:
        matches = [v for v in SUPPORTED_VOICES if voice in v]
        if len(matches) == 1:
            voice = matches[0]
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown voice '{req.voice}'. Available: {SUPPORTED_VOICES}",
            )

    t0 = time.time()
    try:
        # Synthesis must serialise: the ONNX session is not re-entrant.
        async with model_lock:
            samples, sample_rate = await asyncio.to_thread(
                kokoro_instance.create, req.text, voice=voice, speed=req.speed
            )
    except Exception as e:
        logger.error(f"Raw generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    elapsed = time.time() - t0
    logger.info(
        f"Raw generated {len(samples)/sample_rate:.1f}s audio in {elapsed:.2f}s "
        f"(rtf {elapsed/(len(samples)/sample_rate):.3f})"
    )
    return Response(
        content=pcm,
        media_type="audio/pcm",
        headers={
            "X-Sample-Rate": str(sample_rate),
            "X-Duration-S": f"{len(samples)/sample_rate:.3f}",
            "X-Generation-S": f"{elapsed:.3f}",
            "X-Channels": "1",
            "X-Bits": "16",
        },
    )


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    # Guard against path traversal: only serve plain .wav files that resolve
    # to a direct child of AUDIO_DIR.
    if "/" in filename or "\\" in filename or not filename.endswith(".wav"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = (AUDIO_DIR / filename).resolve()
    if filepath.parent != AUDIO_DIR.resolve() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(str(filepath), media_type="audio/wav", filename=filename)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": kokoro_instance is not None,
        "audio_dir": str(AUDIO_DIR),
        "audio_files": len(list(AUDIO_DIR.glob("*.wav"))),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, log_level="info")
