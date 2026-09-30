"""The fixed measures each research lens collects, loaded from measures.json.

A stage module may not read files (it gets everything through the capability object), so the data is loaded here
and the stage imports the result. The file is a draft no human planner has signed off (see its `_doc` line).

  MEASURES  lens -> [{key, means, unit, priority, synonyms}]: the name the extraction step must use, its meaning
            and unit, and the names the model tends to invent instead
  SYNONYM   (lens, invented name) -> the fixed name, used to rewrite an invented name before facts merge
"""

from __future__ import annotations

import json
from pathlib import Path

MEASURES: dict[str, list[dict]] = {
    lens: [dict(m) for m in ms]
    for lens, ms in json.loads((Path(__file__).with_name("measures.json")).read_text())["lenses"].items()}
SYNONYM: dict[tuple[str, str], str] = {(lens, syn): m["key"] for lens, ms in MEASURES.items() for m in ms
                                       for syn in m["synonyms"]}
