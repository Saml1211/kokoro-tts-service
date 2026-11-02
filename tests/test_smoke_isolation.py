"""Exercise the real smoke entry point without loading or calling the real model."""
import hashlib
import os
from pathlib import Path
import shutil
import shlex
import signal
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"


class SmokeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kokoro-smoke-check-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "venv/bin").mkdir(parents=True)
        (self.root / "audio").mkdir()
        shutil.copy2(ROOT / "scripts/smoke_test.sh", self.root / "scripts")
        shutil.copy2(ROOT / "app.py", self.root)
        # Keep real FastAPI, Uvicorn, WAV writing and retention; replace only synthesis.
        (self.root / "kokoro_onnx.py").write_text(
            "import numpy as np\n"
            "class Kokoro:\n"
            " def __init__(self, *args): pass\n"
            " def create(self, *args, **kwargs):\n"
            "  return np.sin(np.arange(2400) * .1).astype('float32') * .1, 24000\n"
        )
        wrapper = self.root / "venv/bin/python"
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(str(PYTHON))} "$@"\n')
        wrapper.chmod(0o755)
        for i in range(101):
            (self.root / "audio" / f"existing-{i:03}.wav").write_bytes(b"RIFF" + bytes([i]))
        self.before = self.audio_snapshot()

    def audio_snapshot(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.root / "audio").iterdir()}

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def smoke(self, port):
        return subprocess.run(
            ["/bin/bash", str(self.root / "scripts/smoke_test.sh")],
            cwd="/tmp", env={**os.environ, "SMOKE_PORT": str(port), "TMPDIR": str(self.root)},
            capture_output=True, text=True, timeout=25,
        )

    def assert_port_closed(self, port):
        with socket.socket() as sock:
            sock.settimeout(.5)
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)

    def test_success_preserves_existing_clips_and_reaps_owned_server(self):
        port = self.free_port()
        result = self.smoke(port)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SMOKE PASS", result.stdout)
        self.assertEqual(self.audio_snapshot(), self.before)
        self.assert_port_closed(port)
        self.assertEqual(list(self.root.glob("kokoro_smoke.*")), [])

    def test_invalid_ports_fail_without_starting_a_server(self):
        for port in (0, -1, 65536, "not-a-port", "1+1"):
            with self.subTest(port=port):
                result = self.smoke(port)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SMOKE_PORT must be between", result.stderr)
                self.assertEqual(self.audio_snapshot(), self.before)
                self.assertEqual(list(self.root.glob("kokoro_smoke.*")), [])

    def test_startup_failure_preserves_clips_and_reaps_owned_socket(self):
        (self.root / "kokoro_onnx.py").write_text("raise RuntimeError('model unavailable')\n")
        port = self.free_port()
        result = self.smoke(port)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model unavailable", result.stderr)
        self.assertEqual(self.audio_snapshot(), self.before)
        self.assert_port_closed(port)
        self.assertEqual(list(self.root.glob("kokoro_smoke.*")), [])

    def test_term_reaps_even_a_model_that_blocks_graceful_shutdown(self):
        started = self.root / "model-started"
        (self.root / "kokoro_onnx.py").write_text(
            "import os, time\nfrom pathlib import Path\n"
            "class Kokoro:\n"
            " def __init__(self, *args):\n"
            "  Path(__file__).with_name('model-started').write_text(str(os.getpid()))\n"
            "  time.sleep(60)\n"
        )
        port = self.free_port()
        proc = subprocess.Popen(
            ["/bin/bash", str(self.root / "scripts/smoke_test.sh")], cwd="/tmp",
            env={**os.environ, "SMOKE_PORT": str(port), "TMPDIR": str(self.root)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        def emergency_cleanup():
            if proc.poll() is None:
                if started.exists():
                    try:
                        os.kill(int(started.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
        self.addCleanup(emergency_cleanup)
        for _ in range(200):
            if started.exists():
                break
            if proc.poll() is not None:
                self.fail("smoke exited before loading model")
            time.sleep(.02)
        else:
            self.fail("model did not start")
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 143, stdout + stderr)
        self.assertEqual(self.audio_snapshot(), self.before)
        self.assert_port_closed(port)
        self.assertEqual(list(self.root.glob("kokoro_smoke.*")), [])

    def test_occupied_port_does_not_call_or_kill_its_listener(self):
        port = self.free_port()
        calls = self.root / "foreign-requests"
        server_code = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  with Path(sys.argv[2]).open('a') as f: f.write(self.path + '\\n')
  self.send_response(200); self.end_headers(); self.wfile.write(b'{"model_loaded":true}')
 def do_POST(self):
  self.do_GET()
 def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
'''
        foreign = subprocess.Popen([str(PYTHON), "-c", server_code, str(port), str(calls)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(foreign.stderr.close)
        def stop_foreign():
            if foreign.poll() is None:
                foreign.terminate()
                foreign.wait(timeout=5)
        self.addCleanup(stop_foreign)
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if foreign.poll() is not None:
                self.fail(foreign.stderr.read().decode())
            time.sleep(.02)
        else:
            self.fail("foreign listener did not start")
        result = self.smoke(port)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(foreign.poll(), "smoke cleanup killed an unrelated listener")
        self.assertFalse(calls.exists(), "smoke contacted the wrong service")
        self.assertEqual(self.audio_snapshot(), self.before)
        self.assertEqual(list(self.root.glob("kokoro_smoke.*")), [])


if __name__ == "__main__":
    unittest.main()
