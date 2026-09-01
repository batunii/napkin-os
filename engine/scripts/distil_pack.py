#!/usr/bin/env python3
"""Distil a knowledge pack into a compact digest.md — the grounding artifact
for deployments with no vector store (the app's default mode).

A digest is 300–600 tokens of PARAPHRASED patterns: what kinds of problems the
pack's cases solve, the strategic moves that recur, and the craft rules worth
holding a brief to. Never verbatim source text — digests are the only pack
artifact that ships publicly, and paraphrase is what makes that shippable
(Cannes/IPA licensing restricts redistributing the raw material).

One offline pass per pack; incremental by corpus hash (a pack whose content
hasn't changed since its digest was written is skipped).

Usage:
    python3 scripts/distil_pack.py [pack_id ...] [--force] [--out packs/]
Env:
    BRIEF_CORPUS   corpus root (pack dirs)
    + the usual provider keys (uses parse_brief's model chain)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "rag"))

from packs import discover_packs, Pack  # noqa: E402
import rag  # noqa: E402
import parse_brief  # noqa: E402

DIGEST_PROMPT = """You are distilling a corpus of {kind} material ("{pack}") into a compact
strategy digest for an advertising-brief writer. You will see samples of the corpus.

Write 300-600 tokens of PARAPHRASED patterns — never quote or closely track the
source text. Structure:

## What great looks like
3-5 bullet patterns: the strategic moves that recur in strong work here
(problem type -> the reframe that unlocked it -> why it worked).

## Craft rules for a brief
3-5 bullet rules a brief should obey, drawn from these patterns.

## Traps
2-3 bullet failure modes this corpus warns against.

Concrete but abstracted: name mechanisms and problem shapes, not brands or
campaigns. Output markdown only, starting with '## What great looks like'."""


def corpus_fingerprint(pack: Pack) -> str:
    h = hashlib.sha256()
    assert pack.path is not None
    for f in sorted(pack.path.rglob("*.md")):
        h.update(f.name.encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()[:16]


def sample_pack(pack: Pack, budget_chars: int = 24000) -> str:
    """Spread the sample across the pack rather than reading the head of it."""
    assert pack.path is not None
    files = sorted(p for p in pack.path.rglob("*.md") if "DROP-ZIPS-HERE" not in p.name)
    if not files:
        return ""
    step = max(1, len(files) // 24)
    picked = files[::step][:24]
    per = max(500, budget_chars // max(1, len(picked)))
    parts = []
    for f in picked:
        for c in rag.chunk_file(f)[:2]:
            parts.append(f"### {c['section']}\n{c['text'][:per]}")
    return "\n\n".join(parts)[:budget_chars]


def distil(pack: Pack, out_dir: Path, force: bool = False) -> str:
    out = out_dir / pack.id / "digest.md"
    state = out_dir / pack.id / ".digest_state.json"
    fp = corpus_fingerprint(pack)
    if out.exists() and state.exists() and not force:
        if json.loads(state.read_text()).get("fingerprint") == fp:
            return "unchanged"
    sample = sample_pack(pack)
    if not sample:
        return "empty"
    prompt = DIGEST_PROMPT.format(kind=pack.kind, pack=pack.id) + "\n\nCORPUS SAMPLE:\n" + sample
    text = parse_brief._chat(  # same model chain + retry/failover as the pipeline
        prompt, system="You distil strategy corpora into short, original pattern digests.",
        max_tokens=1200)
    if not text or len(text.strip()) < 200:
        return "failed"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text.strip() + "\n")
    state.write_text(json.dumps({"fingerprint": fp, "pack": pack.id, "kind": pack.kind}))
    return "written"


def main(argv=None):
    ap = argparse.ArgumentParser(description="distil packs into shippable digests")
    ap.add_argument("packs", nargs="*", help="pack ids (default: all)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default=str(ENGINE / "packs_dist"),
                    help="output root (default: engine/packs_dist)")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    packs = discover_packs()
    if args.packs:
        packs = [p for p in packs if p.id in args.packs]
        missing = set(args.packs) - {p.id for p in packs}
        if missing:
            sys.exit(f"unknown packs: {sorted(missing)}")
    for p in packs:
        if p.path is None:
            print(f"{p.id}: skipped (no corpus on disk — digests are built where the corpus lives)")
            continue
        print(f"{p.id}: {distil(p, out_dir, force=args.force)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
