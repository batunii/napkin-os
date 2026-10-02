import socket
import threading
import time

import httpx
import pytest
import uvicorn

from napkin.app import create_app
from napkin.config import Settings
from napkin.layers.http import HttpLayerStore

from fake_layers import FakeLayersService
from fakes import FakeModel, FakeResearch


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LAYERS_URL = "http://layers.test"


def layer_store(service: FakeLayersService | None = None) -> HttpLayerStore:
    """The middleware's own layers client, over an in-process transport to a
    fake napkin.layers/1 service."""
    service = service or FakeLayersService()
    st = HttpLayerStore(LAYERS_URL, transport=service.transport(), sleep=lambda s: None)
    st.service = service
    return st


@pytest.fixture
def layers_service():
    return FakeLayersService()


@pytest.fixture
def store(layers_service):
    return layer_store(layers_service)


@pytest.fixture
def layers(store):
    return store.open({"org": "org/test-agency", "brand": "brand/test"})


class Server:
    def __init__(self, tmp_path, model=None, research=None, retrieval=None, settings_kw=None, jev=None):
        from napkin.config import _default_pipelines
        self.model, self.research = model or FakeModel(), research or FakeResearch()
        self.retrieval = retrieval
        self.layers = layer_store()
        # the field-by-field brief flow unless a test asks for the planned one (test_planned_brief.py)
        settings = Settings(pipelines=_default_pipelines(), port=free_port(),
                            **{"brief_flow": "fields", **(settings_kw or {})})
        self.app = create_app(settings, model_client=self.model, research_port=self.research,
                              layer_store=self.layers, retrieval_port=retrieval, jev_port=jev)
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
