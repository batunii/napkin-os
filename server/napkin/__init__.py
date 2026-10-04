"""The Napkin middleware: `napkin.middleware/1` (docs/contracts/middleware-api.md).

The middleware holds the logic — stage orchestration, the identify rules,
tiering, derived confidence, the per-market merge, contests, the cite rule,
Brief Maker's capture, drafters and Judge — and talks to four peripherals
through ports (docs/contracts/peripherals.md): the model (Anthropic Messages,
direct or on Amazon Bedrock, or an OpenAI-compatible endpoint), research, retrieval and the knowledge
layers (all HTTP). Which implementation sits behind each port is
configuration only.
"""

API = "napkin.middleware/1"
BACKEND_BASE = "napkin-middleware/0.1"
_WIRE = {"api": "anthropic"}


def set_model_wire(api: str) -> None:
    """Which model wire this process speaks: recorded as trace.backend's model
    half (peripherals.md §1.1). Set once at startup."""
    _WIRE["api"] = api


def backend() -> str:
    return f"{BACKEND_BASE}+model-{_WIRE['api']}"
