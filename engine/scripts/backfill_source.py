#!/usr/bin/env python3
"""Back-fill the unified `source:` frontmatter key into existing corpus markdown.

The RAG was originally built without a top-level corpus discriminator. As we add
Cannes/Effie/D&AD, every chunk needs a `source` so retrieval can filter by corpus
(where={"source": "cannes"}) or blend across them. New ingest scripts write `source`
directly; this one-time, idempotent pass adds it to the pre-existing files.

Mapping is by directory under reference/rag/:
  ipa/**              -> ipa
  playbooks/**        -> playbook
  briefing-template/**-> template

Run from the repo root (scripts/.. ) — or anywhere; paths are resolved off this file.
Re-running is safe: files that already have a `source:` line in their frontmatter are skipped.
Rebuild the index afterwards (rag/build_rag.sh) for the change to reach the index.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # briefing/
CORPUS = ROOT / "reference" / "rag"

# (relative subdir, source value) — longest/most-specific paths first
RULES = [
    ("ipa", "ipa"),
    ("playbooks", "playbook"),
    ("briefing-template", "template"),
]


def source_for(path: Path) -> str | None:
    rel = path.relative_to(CORPUS).as_posix()
    for subdir, src in RULES:
        if rel.startswith(subdir + "/"):
            return src
    return None


def backfill(path: Path, src: str) -> bool:
    """Insert `source: <src>` right after the opening `---`. Returns True if changed."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False                    # no frontmatter block; leave untouched
    lines = text.splitlines(keepends=True)
    # find the frontmatter close (second '---')
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return False
    front = lines[1:close]
    if any(l.strip().startswith("source:") for l in front):
        return False                    # already has source — idempotent skip
    newline = "\n" if lines[0].endswith("\n") else ""
    lines.insert(1, f"source: {src}{newline}")
    path.write_text("".join(lines), encoding="utf-8")
    return True


def main():
    if not CORPUS.is_dir():
        sys.exit(f"corpus dir not found: {CORPUS}")
    changed = skipped = 0
    by_source: dict[str, int] = {}
    for md in sorted(CORPUS.rglob("*.md")):
        if "DROP-ZIPS-HERE" in str(md):
            continue
        src = source_for(md)
        if src is None:
            continue
        if backfill(md, src):
            changed += 1
            by_source[src] = by_source.get(src, 0) + 1
        else:
            skipped += 1
    print(f"backfilled source into {changed} files (skipped {skipped} already-tagged/no-frontmatter)")
    for src, n in sorted(by_source.items()):
        print(f"  source={src}: {n}")
    print("Now rebuild the index: cd rag && ./build_rag.sh")


if __name__ == "__main__":
    main()
