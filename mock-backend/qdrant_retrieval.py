"""napkin.retrieval/1 served from Sai's Qdrant index (the engine's own retrieval: engine/rag/retrieve.py).

  GET  /v1/packs      the indexed packs (engine/rag/packs.lock): tag, kind, k, loops, version
  POST /v1/retrieve   {query, k, packs?, where?, purpose?} -> verbatim passages, per pack

The mock's retrieval port quotes from 3-passage digests of each pack; this one searches the
whole corpora the engine indexed (IPA, Cannes, D&AD, playbooks, the briefing template) with
the engine's hybrid search, its brief-safe filter and its scopes. Keys come from engine/.env
(the engine's loader): QDRANT_*, NVIDIA_API_KEY, RAG_STORE=qdrant.

Run:  uv run --no-project --with numpy --with requests --with httpx --with PyYAML --with python-dotenv \\
        python mock-backend/qdrant_retrieval.py            # :8786, or QDRANT_RETRIEVAL_PORT
Point the middleware at it: NAPKIN_RETRIEVAL_URL=http://127.0.0.1:8786
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path[:0] = [str(ENGINE), str(ENGINE / "rag")]
import engine_env  # noqa: E402

engine_env.load()
import retrieve  # noqa: E402

API = "napkin.retrieval/1"
MAX_TEXT = 4000
LOCK = json.loads((ENGINE / "rag" / "packs.lock").read_text())
LABEL = retrieve.index_label()
PACKS = [{"tag": p["tag"], "id": p["id"], "kind": p["kind"], "scope": "house", "licence": "licensed-internal",
          "k": int(p.get("k") or 2), "loops": list(p.get("loops") or []), "passages": int(p.get("chunks") or 0),
          "filterable": [], "version": "sha256:" + hashlib.sha256(f"{LABEL}|{LOCK.get('embed_model')}|{p['id']}"
                                                                  .encode()).hexdigest()}
         for p in LOCK["packs"]]
BY_TAG = {p["tag"]: p for p in PACKS}
# the index's metadata `source` for each pack tag
SOURCE = {"template": "template", "cannes": "cannes", "dandad": "dandad", "ipa": "ipa", "playbook": "playbook"}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def section_slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def passage(pack, hit):
    text = str(hit.get("text") or "")[:MAX_TEXT].strip()
    source, section = str(hit.get("source") or "index"), str(hit.get("section") or "-")
    sha = hashlib.sha256(text.encode()).hexdigest()
    pid = "psg_" + hashlib.sha256(f"{pack}\n{source}\n{section}\n{text}".encode()).hexdigest()[:20]
    return {"id": pid, "uri": f"passage://{pack}/{source}#{section_slug(section)}@{sha[:16]}", "pack": pack,
            "scope": "house", "licence": "licensed-internal", "source": source, "section": section,
            "citation": str(hit.get("citation") or f"{source} › {section}"), "text": text, "text_sha256": sha,
            "score": float(hit.get("score") or 0)}


class H(BaseHTTPRequestHandler):
    def _send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/packs":
            return self._send(200, {"packs": PACKS, "embed_model": LOCK.get("embed_model"), "backend": f"qdrant:{LABEL}"})
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True, "index": LABEL})
        self._send(404, {"error": {"type": "not_found", "message": "no such route"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/retrieve":
            return self._send(404, {"error": {"type": "not_found", "message": "no such route"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            q = re.sub(r"\s+", " ", str(body.get("query") or "")).strip()
            k = max(1, min(20, int(body.get("k") or 5)))
            tags = [t for t in (body.get("packs") or list(BY_TAG)) if t in BY_TAG]
            if not q:
                return self._send(400, {"error": {"type": "invalid_request", "message": "query is empty"}})
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": {"type": "invalid_request", "message": str(e)[:200]}})
        t0, out = time.monotonic(), []
        try:
            for t in tags:
                for attempt in (1, 2, 3):  # a read cut short by the store or the embedder is retried
                    try:
                        hits = retrieve.retrieve(q, k=k, where={"source": SOURCE.get(t, t)})
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt == 3:
                            raise
                        log(f"retrieve {t} attempt {attempt}: {type(e).__name__}; retrying")
                        time.sleep(1.5 * attempt)
                for h in hits:
                    p = passage(t, h)
                    if p["text"]:
                        out.append(p)
        except Exception as e:  # noqa: BLE001 - the store failed: a 502, never an empty success
            log(f"retrieve failed: {type(e).__name__}: {str(e)[:200]}")
            return self._send(502, {"error": {"type": "upstream", "message": f"{type(e).__name__}"}})
        seen, best = set(), []
        for p in sorted(out, key=lambda x: -x["score"]):
            if p["id"] not in seen:
                seen.add(p["id"])
                best.append({k_: v for k_, v in p.items() if k_ != "score"})
        best = best[:k]
        log(f"retrieve {body.get('purpose') or '-'} packs={','.join(tags)} k={k}: {len(best)} passage(s) "
            f"in {time.monotonic() - t0:.1f}s")
        self._send(200, {"passages": best, "trace": {"backend": f"qdrant:{LABEL}",
                                                     "packs": {t: BY_TAG[t]["version"] for t in tags}}})


if __name__ == "__main__":
    port = int(os.environ.get("QDRANT_RETRIEVAL_PORT") or 8786)
    log(f"{API} on http://127.0.0.1:{port} from {LABEL}: packs {', '.join(BY_TAG)}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
