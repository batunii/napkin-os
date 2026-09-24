"""The real mock backend in a subprocess, pointed at tests/fake_claude.py, on a
free port (never one of the owner's). Spends nothing."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / "server.py"
CONTRACT = ROOT / "contract"
FAKE = HERE / "fake_claude.py"
RESERVED = {8080, 8090, 8787, 8788, 8790, 8791, 8792, 8795, 8796, 8797}

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CONTRACT))


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p not in RESERVED:
            return p


class Server:
    def __init__(self, **env):
        os.chmod(FAKE, 0o755)
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory(prefix="mock-backend-test-")
        t = Path(self.tmp.name)
        self.log_dir = t / "log"
        self.log_dir.mkdir()
        self.data = t / "data"                 # MOCK_DATA: cache, work, layers.sqlite
        self.scratch = t / "scratch"           # the server's cwd and TMPDIR
        self.scratch.mkdir()
        self.stderr_path = t / "stderr.log"
        full = {k: v for k, v in os.environ.items()
                if not k.startswith(("MOCK_", "FAKE_")) and k not in ("BRIEF_CORPUS",)}
        full.update(MOCK_BACKEND_PORT=str(self.port), MOCK_CLAUDE_BIN=str(FAKE), MOCK_DATA=str(self.data),
                    FAKE_CLAUDE_LOG=str(self.log_dir), TMPDIR=str(self.scratch))
        full.update({k: str(v) for k, v in env.items()})
        self._stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen([sys.executable, str(SERVER)], cwd=self.scratch, env=full,
                                     stdout=subprocess.DEVNULL, stderr=self._stderr)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(200):
            if self.proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(self.base + "/healthz", timeout=1).read()
                return
            except urllib.error.HTTPError as e:
                e.close()
                return          # up (e.g. 401 with a token)
            except OSError:
                time.sleep(0.05)
        self.close()
        raise RuntimeError(f"mock backend did not start: {self.stderr()}")

    def call(self, method, path, body=None, raw=None, headers=None, timeout=30):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"content-type": "application/json"}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null"), dict(e.headers.items())

    def post(self, path, body, **kw):
        return self.call("POST", path, body, **kw)[:2]

    def get(self, path, **kw):
        return self.call("GET", path, **kw)[:2]

    def runs(self):
        return [json.loads(p.read_text()) for p in self.log_dir.glob("*.json")]

    def stderr(self) -> str:
        try:
            return self.stderr_path.read_text(errors="replace")
        except OSError:
            return ""

    def run_contract(self, script: str, *args, timeout=120):
        return subprocess.run([sys.executable, str(CONTRACT / script), "--base-url", self.base, *args],
                              capture_output=True, text=True, timeout=timeout)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self._stderr.close()
        self.tmp.cleanup()


def files_containing(root: Path, needle: bytes) -> list:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if needle in p.read_bytes():
                    hits.append(p)
            except OSError:
                pass
    return hits
