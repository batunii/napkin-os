"""Configuration, from the environment only. Handlers never see any of it.

  ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY   read by the SDK itself (the model port)
  NAPKIN_MODEL                 model id (claude-opus-5)
  NAPKIN_MODEL_TIMEOUT         seconds per model call (600)
  NAPKIN_RESEARCH_URL          research service root; unset = research units fail as gaps
  NAPKIN_RESEARCH_TIMEOUT      seconds per research call (900)
  NAPKIN_RESEARCH_CONCURRENCY  research units in flight at once (4)
  NAPKIN_REUSE_DAYS            a layer fact younger than this is reused, not re-researched (30)
  NAPKIN_LAYERS                local:/path/layers.sqlite (default local:<server>/var/layers.sqlite)
  NAPKIN_DEV_ORG / NAPKIN_DEV_BRAND  the fixed development tenant (org/dev-agency, brand/dev-brand)
  NAPKIN_TOKEN                 when set, requests must carry it (Bearer or x-api-key)
  NAPKIN_PIPELINES             ':'-separated pipeline.yaml files resolved at startup
                               (default: every app/templates/*/app/pipeline.yaml in the repo)
  NAPKIN_HOST / NAPKIN_PORT    bind address (127.0.0.1:8795)
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent


def _default_pipelines() -> list[str]:
    return sorted(glob.glob(str(SERVER_DIR.parent / "app" / "templates" / "*" / "app" / "pipeline.yaml")))


@dataclass
class Settings:
    model: str = "claude-opus-5"
    model_timeout: float = 600.0
    research_url: str | None = None
    research_timeout: float = 900.0
    research_concurrency: int = 4
    reuse_days: int = 30
    layers: str = ""
    org: str = "org/dev-agency"
    brand: str = "brand/dev-brand"
    token: str | None = None
    pipelines: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8795

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        layers = e("NAPKIN_LAYERS")
        if not layers:
            (SERVER_DIR / "var").mkdir(exist_ok=True)
            layers = "local:" + str(SERVER_DIR / "var" / "layers.sqlite")
        pipes = e("NAPKIN_PIPELINES")
        return cls(model=e("NAPKIN_MODEL") or "claude-opus-5",
                   model_timeout=float(e("NAPKIN_MODEL_TIMEOUT") or 600),
                   research_url=e("NAPKIN_RESEARCH_URL") or None,
                   research_timeout=float(e("NAPKIN_RESEARCH_TIMEOUT") or 900),
                   research_concurrency=int(e("NAPKIN_RESEARCH_CONCURRENCY") or 4),
                   reuse_days=int(e("NAPKIN_REUSE_DAYS") or 30),
                   layers=layers, org=e("NAPKIN_DEV_ORG") or "org/dev-agency",
                   brand=e("NAPKIN_DEV_BRAND") or "brand/dev-brand", token=e("NAPKIN_TOKEN") or None,
                   pipelines=[p for p in pipes.split(":") if p] if pipes is not None else _default_pipelines(),
                   host=e("NAPKIN_HOST") or "127.0.0.1", port=int(e("NAPKIN_PORT") or 8795))

    @property
    def scope(self) -> dict:
        return {"org": self.org, "brand": self.brand}
