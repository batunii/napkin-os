import socket
import threading
import time

import httpx
import pytest
import uvicorn

from napkin.app import create_app
from napkin.config import Settings
from napkin.layers.local import LocalLayerStore

from fakes import FakeModel, FakeResearch


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_settings(tmp_path, **kw):
    s = Settings(layers=f"local:{tmp_path / 'layers.sqlite'}", pipelines=Settings.from_env().pipelines
                 if False else [], **kw)
    return s


@pytest.fixture
def store(tmp_path):
    return LocalLayerStore(str(tmp_path / "layers.sqlite"))


@pytest.fixture
def layers(store):
    return store.open({"org": "org/test-agency", "brand": "brand/test"})


class Server:
    def __init__(self, tmp_path, model=None, research=None):
        from napkin.config import _default_pipelines
        self.model, self.research = model or FakeModel(), research or FakeResearch()
        settings = Settings(layers=f"local:{tmp_path / 'layers.sqlite'}", pipelines=_default_pipelines(),
                            port=free_port())
        self.app = create_app(settings, model_client=self.model, research_port=self.research)
        self.url = f"http://127.0.0.1:{settings.port}"
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=settings.port, log_level="warning")
        self.srv = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.srv.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            try:
                if httpx.get(self.url + "/healthz").status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.02)
        raise RuntimeError("server did not start")

    def stop(self):
        self.srv.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path)
    yield s
    s.stop()


REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def contract_suite():
    """The napkin.middleware/1 contract suite (docs/contracts/middleware-api.md §7),
    found by what it tests rather than by where the stand-in keeps it."""
    for p in sorted(REPO.glob("*/contract_test.py")):
        if 'API = "napkin.middleware/1"' in p.read_text():
            return p
    raise FileNotFoundError("the napkin.middleware/1 contract suite")
