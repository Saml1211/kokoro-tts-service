"""Pin the audio retention behaviour: keep the newest N clips, drop the rest."""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


def make_wavs(tmp: Path, n: int):
    for i in range(n):
        p = tmp / f"kokoro_test_{i}.wav"
        p.write_bytes(b"RIFF")
        os.utime(p, (time.time() - (n - i) * 10,) * 2)  # oldest first


def test_cleanup_keeps_newest_n():
    tmp = Path(tempfile.mkdtemp())
    original = app.AUDIO_DIR
    try:
        app.AUDIO_DIR = tmp
        make_wavs(tmp, 5)
        removed = app.cleanup_audio(keep=3)
        assert removed == 2
        remaining = sorted(p.name for p in tmp.glob("*.wav"))
        assert remaining == ["kokoro_test_2.wav", "kokoro_test_3.wav", "kokoro_test_4.wav"]
        # Under the cap: nothing to do, nothing deleted.
        assert app.cleanup_audio(keep=3) == 0
    finally:
        app.AUDIO_DIR = original
