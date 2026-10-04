"""One structured call through the configured model port, to prove a wire
before real work runs on it (auth, region, model id, structured output).

    cd server && NAPKIN_MODEL_API=bedrock NAPKIN_MODEL_REGION=eu-west-1 \
        uv run python scripts/model_smoke.py

Prints the object and the usage, or the ModelError kind. A few hundred tokens.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from napkin.config import Settings  # noqa: E402
from napkin.model import ModelError, ModelPort, Usage, build_wire  # noqa: E402

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["capital", "country", "note"],
          "properties": {"capital": {"type": "string"}, "country": {"type": "string", "enum": ["IE", "GB", "FR"]},
                         "note": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}


def main() -> int:
    s = Settings.from_env()
    port = ModelPort(build_wire(s), s.model, s.model_timeout, max_tokens=1024)
    u = Usage()
    try:
        out = port.call("smoke", "Answer with the requested JSON only.", {"question": "Capital of Ireland?"},
                        SCHEMA, usage=u, attribution="smoke")
    except ModelError as e:
        print(json.dumps({"wire": port.api, "model": s.model, "error": e.kind, "message": str(e)}))
        return 1
    print(json.dumps({"wire": port.api, "model": s.model, "result": out, "usage": u.as_dict()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
