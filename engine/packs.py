"""Knowledge packs: convention over configuration.

A pack IS a directory under the corpus root. dirname = pack id = the
`source` tag its chunks carry (overridable via pack.yaml for legacy tags).
Directory exists -> pack exists; `_`-prefixed dirname -> deactivated.
There is NO hand-maintained registry: the only list of packs is either the
filesystem itself, or `packs.lock` — a build artifact written exclusively by
`napkin-packs sync`, shipped where the corpus doesn't travel (the app).

Discovery order:
  1. corpus dirs on disk   (dev: the corpus repo)          — authoritative
  2. packs.lock            (runtime: app / CI, no corpus)  — what sync indexed

Optional per-pack overrides in <pack>/pack.yaml (flat key: value):
  tag: playbook            # index tag differs from dirname (legacy corpora)
  kind: case | playbook | template     (default: case)
  k: 2                     # per-loop precedent pull depth (default: 2)
  loops: [loop4_insight, loop6_substantiation]   # omitted = all loops
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ENGINE = Path(__file__).resolve().parent

# Loops that pull per-pack precedent today (parse_brief loops 4/6).
DEFAULT_CASE_LOOPS = ("loop4_insight", "loop6_substantiation")


@dataclass
class Pack:
    id: str                      # dirname
    tag: str                     # metadata.source value in the index
    kind: str = "case"           # case | playbook | template
    k: int = 2
    loops: tuple = ()            # empty = all loops
    path: Path | None = None     # None when discovered from packs.lock
    chunks: int | None = None    # known only from packs.lock / sync
    active: bool = True

    def eligible(self, loop_key: str) -> bool:
        return self.active and (not self.loops or loop_key in self.loops)


def _parse_flat_yaml(text: str) -> dict:
    """pack.yaml is deliberately flat; parse without a yaml dependency."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        pass
    out: dict = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip("'\"")
        if val.startswith("[") and val.endswith("]"):
            out[key.strip()] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val.isdigit():
            out[key.strip()] = int(val)
        else:
            out[key.strip()] = val
    return out


def corpus_root() -> Path | None:
    """Corpus location: $BRIEF_CORPUS, else the conventional spots."""
    env = os.environ.get("BRIEF_CORPUS")
    candidates = [Path(env)] if env else [
        ENGINE / "reference" / "rag",
        ENGINE.parent / "reference" / "rag",
    ]
    for c in candidates:
        if c.is_dir() and any(p.is_dir() for p in c.iterdir()):
            return c
    return None


def lock_path() -> Path:
    return Path(os.environ.get("BRIEF_PACKS_LOCK", ENGINE / "rag" / "packs.lock"))


def _pack_from_dir(d: Path) -> Pack:
    cfg = {}
    py = d / "pack.yaml"
    if py.is_file():
        cfg = _parse_flat_yaml(py.read_text())
    kind = cfg.get("kind", "case")
    loops = tuple(cfg.get("loops", DEFAULT_CASE_LOOPS if kind == "case" else ()))
    return Pack(
        id=d.name,
        tag=str(cfg.get("tag", d.name)),
        kind=kind,
        k=int(cfg.get("k", 2)),
        loops=loops,
        path=d,
        active=not d.name.startswith("_"),
    )


def discover_packs(root: Path | None = None) -> list[Pack]:
    """Disk first; packs.lock as the runtime fallback. Never both."""
    root = root or corpus_root()
    if root is not None:
        packs = []
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            p = _pack_from_dir(d)
            if p.active:
                packs.append(p)
        return packs

    lp = lock_path()
    if lp.is_file():
        data = json.loads(lp.read_text())
        return [
            Pack(id=e["id"], tag=e["tag"], kind=e.get("kind", "case"),
                 k=int(e.get("k", 2)), loops=tuple(e.get("loops", [])),
                 chunks=e.get("chunks"))
            for e in data.get("packs", [])
        ]
    return []


def packs_for_loop(loop_key: str, kind: str | None = None,
                   root: Path | None = None) -> list[Pack]:
    packs = [p for p in discover_packs(root) if p.eligible(loop_key)]
    if kind:
        packs = [p for p in packs if p.kind == kind]
    return packs


def write_lock(packs: list[Pack], *, embed_model: str, dim: int,
               counts: dict[str, int] | None = None, path: Path | None = None) -> Path:
    """Written ONLY by `napkin-packs sync`. Hand-editing this file is a bug."""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_by": "napkin-packs sync",
        "note": "build artifact — regenerate with sync, never edit by hand",
        "embed_model": embed_model,
        "dim": dim,
        "packs": [
            {"id": p.id, "tag": p.tag, "kind": p.kind, "k": p.k,
             "loops": list(p.loops), "chunks": (counts or {}).get(p.tag, p.chunks)}
            for p in packs
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
