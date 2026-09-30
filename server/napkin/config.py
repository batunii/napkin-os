"""Configuration, from the environment only (peripherals.md §7). Handlers never
see any of it.

  NAPKIN_MODEL_API             anthropic (default) | openai — the model port's wire shape
  NAPKIN_MODEL_BASE_URL        anthropic: the API root (unset = the SDK's own resolution,
                               ANTHROPIC_BASE_URL); openai: the root /chat/completions is
                               appended to, including /v1 (required)
  NAPKIN_MODEL_API_KEY         anthropic: unset = the SDK's resolution; openai: the bearer key
  NAPKIN_MODEL                 model id, sent verbatim (claude-opus-5-5)
  NAPKIN_VISION_MODEL          model id for image transcription (= NAPKIN_MODEL)
  NAPKIN_MODEL_EXTRA_BODY      openai only: a JSON object merged into every request body
  NAPKIN_MODEL_TIMEOUT         seconds per HTTP attempt (600)
  NAPKIN_MODEL_CONCURRENCY     model calls in flight at once, across jobs (8)
  NAPKIN_RESEARCH_URL          research service root; unset = research units fail as gaps
  NAPKIN_RESEARCH_TOKEN        its bearer token
  NAPKIN_RESEARCH_TIMEOUT      seconds per research call (900)
  NAPKIN_RESEARCH_CONCURRENCY  research units in flight at once (8)
  NAPKIN_RETRIEVAL_URL         retrieval service root; unset = drafters get no passages
  NAPKIN_RETRIEVAL_TOKEN       its bearer token
  NAPKIN_RETRIEVAL_TIMEOUT     seconds per retrieval call (120)
  NAPKIN_LAYERS_URL            the layers service root (required to serve)
  NAPKIN_LAYERS_TOKEN          its bearer token
  NAPKIN_LAYERS_TIMEOUT        seconds per layers call (30)
  NAPKIN_REUSE_DAYS            a layer fact younger than this is reused, not re-researched (30)
  NAPKIN_DEV_ORG / NAPKIN_DEV_BRAND  the fixed development tenant (org/dev-agency, brand/dev-brand)
  NAPKIN_TOKEN                 when set, requests must carry it (Bearer or x-api-key)
  NAPKIN_PIPELINES             ':'-separated pipeline.yaml files resolved at startup
                               (default: every app/templates/*/app/pipeline.yaml in the repo)
  NAPKIN_HOST / NAPKIN_PORT    bind address (127.0.0.1:8795)
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent


def _default_pipelines() -> list[str]:
    return sorted(glob.glob(str(SERVER_DIR.parent / "app" / "templates" / "*" / "app" / "pipeline.yaml")))


@dataclass
class Settings:
    model_api: str = "anthropic"
    model_base_url: str | None = None
    model_api_key: str | None = None
    model: str = "claude-opus-5-5"
    vision_model: str | None = None
    model_extra_body: dict = field(default_factory=dict)
    model_timeout: float = 600.0
    model_concurrency: int = 8
    research_url: str | None = None
    research_token: str | None = None
    research_timeout: float = 900.0
    research_concurrency: int = 8
    retrieval_url: str | None = None
    retrieval_token: str | None = None
    retrieval_timeout: float = 120.0
    layers_url: str | None = None
    layers_token: str | None = None
    layers_timeout: float = 30.0
    reuse_days: int = 30
    org: str = "org/dev-agency"
    brand: str = "brand/dev-brand"
    token: str | None = None
    pipelines: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8795

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        pipes = e("NAPKIN_PIPELINES")
        extra = e("NAPKIN_MODEL_EXTRA_BODY")
        try:
            extra_body = json.loads(extra) if extra else {}
        except json.JSONDecodeError as err:
            raise SystemExit(f"NAPKIN_MODEL_EXTRA_BODY is not JSON: {err.msg}") from None
        if not isinstance(extra_body, dict):
            raise SystemExit("NAPKIN_MODEL_EXTRA_BODY must be a JSON object")
        return cls(model_api=(e("NAPKIN_MODEL_API") or "anthropic").strip().lower(),
                   model_base_url=e("NAPKIN_MODEL_BASE_URL") or None,
                   model_api_key=e("NAPKIN_MODEL_API_KEY") or None,
                   model=e("NAPKIN_MODEL") or "claude-opus-5-5",
                   vision_model=e("NAPKIN_VISION_MODEL") or None,
                   model_extra_body=extra_body,
                   model_timeout=float(e("NAPKIN_MODEL_TIMEOUT") or 600),
                   model_concurrency=int(e("NAPKIN_MODEL_CONCURRENCY") or 8),
                   research_url=e("NAPKIN_RESEARCH_URL") or None,
                   research_token=e("NAPKIN_RESEARCH_TOKEN") or None,
                   research_timeout=float(e("NAPKIN_RESEARCH_TIMEOUT") or 900),
                   research_concurrency=int(e("NAPKIN_RESEARCH_CONCURRENCY") or 8),
                   retrieval_url=e("NAPKIN_RETRIEVAL_URL") or None,
                   retrieval_token=e("NAPKIN_RETRIEVAL_TOKEN") or None,
                   retrieval_timeout=float(e("NAPKIN_RETRIEVAL_TIMEOUT") or 120),
                   layers_url=e("NAPKIN_LAYERS_URL") or None,
                   layers_token=e("NAPKIN_LAYERS_TOKEN") or None,
                   layers_timeout=float(e("NAPKIN_LAYERS_TIMEOUT") or 30),
                   reuse_days=int(e("NAPKIN_REUSE_DAYS") or 30),
                   org=e("NAPKIN_DEV_ORG") or "org/dev-agency",
                   brand=e("NAPKIN_DEV_BRAND") or "brand/dev-brand", token=e("NAPKIN_TOKEN") or None,
                   pipelines=[p for p in pipes.split(":") if p] if pipes is not None else _default_pipelines(),
                   host=e("NAPKIN_HOST") or "127.0.0.1", port=int(e("NAPKIN_PORT") or 8795))

    @property
    def scope(self) -> dict:
        return {"org": self.org, "brand": self.brand}
