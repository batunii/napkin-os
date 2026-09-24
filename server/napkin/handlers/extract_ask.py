"""extract_ask@1 — the ask, read from the material in one response (short task)."""

from ..pipeline.extract import run_extract

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "extract_ask", "extract_ask", "1.0", "short", 1


def run(req, caps):
    """-> (result, change, hits)."""
    return run_extract(req.doc, req.base, req.clan, req.inp, req.handler, caps)
