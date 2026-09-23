#!/usr/bin/env python3
"""
labelpage.py — a local page for confirming the step-6 pre-labels.

    python3 labelpage.py        # writes golden/labels/client/label_page.html, open it in a browser

One self-contained HTML file: the client set first (closest to production), then the IPA
set. Each card shows the brief, the retrieved chunk, and Sonnet's proposal with its
reason; you confirm "useful" (Y) or "not useful" (N), or skip. Decisions are kept in the
browser (localStorage) so the page can be closed and reopened, and "Export" downloads
confirmed_labels.json, which `python3 labelset.py confirm <file>` merges back.

The page is written into golden/labels/client/, which is git-ignored: it contains real
client material and must stay on this machine. It loads nothing from the network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = HERE / "golden" / "labels"
OUT = LABELS / "client" / "label_page.html"


def _rows(folder: Path, set_name: str) -> list[dict]:
    """Cards for one set: pair + brief + pre-label, in brief then bucket then rank order."""
    load = lambda f: [json.loads(l) for l in (folder / f).read_text().splitlines() if l.strip()] \
        if (folder / f).exists() else []
    briefs = {b["doc_id"]: b for b in load("briefs.jsonl")}
    pre = {(l["brief_id"], l["cite"], l["bucket"]): l for l in load("prelabels.jsonl")}
    out = []
    for p in load("pairs.jsonl"):
        b, l = briefs.get(p["brief_id"]), pre.get((p["brief_id"], p["cite"], p["bucket"]))
        if not b or not l:
            continue
        out.append({"key": f"{set_name}|{p['brief_id']}|{p['bucket']}|{p['cite']}", "set": set_name,
                    "brief_id": p["brief_id"], "bucket": p["bucket"], "cite": p["cite"],
                    "brief": f"{b['brand']} ({b['category']}) — Problem: {b['problem']} Objective: "
                             f"{b['objective']} Audience: {b['audience']}",
                    "text": p["text"], "proposal": l["useful"], "why": l.get("why", "")})
    return out


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Label confirmation</title>
<style>
:root{--bg:#fafaf7;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--rule:#e2e2dc;--yes:#2f7a52;--no:#b3412f;--acc:#2e5c8a}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--fg:#e8e8e3;--mut:#9a9a93;--card:#1d1d1a;--rule:#33332f;--yes:#5fb487;--no:#e07a66;--acc:#7fb0e0}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--rule);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
main{max-width:860px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:14px 16px;margin:0 0 14px}
.card.cur{outline:2px solid var(--acc)}
.meta{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.brief{font-size:13px;color:var(--mut);margin:6px 0 10px}
pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,Menlo,monospace;background:var(--bg);padding:8px 10px;border-radius:6px;max-height:220px;overflow:auto;margin:0}
.prop{margin:10px 0 8px;font-size:14px}.prop b.y{color:var(--yes)}.prop b.n{color:var(--no)}
button{font:inherit;border:1px solid var(--rule);background:var(--card);color:var(--fg);border-radius:6px;padding:5px 12px;cursor:pointer}
button.y.on{background:var(--yes);color:#fff;border-color:var(--yes)}button.n.on{background:var(--no);color:#fff;border-color:var(--no)}
select{font:inherit}
</style></head><body>
<header><strong>Label confirmation</strong><span id="prog"></span>
<label>Show <select id="flt"><option value="all">all</option><option value="todo">unconfirmed</option><option value="client">client set</option><option value="ipa">IPA set</option></select></label>
<button id="exp">Export confirmed_labels.json</button><span class="meta">Y useful · N not useful · J/K next/prev</span></header>
<main id="list"></main>
<script>
const ROWS = __ROWS__;
const KEY = "napkin-labels-v1";
let st = {}; try { st = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} };
const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
let cur = 0, shown = [];
function render() {
  const f = document.getElementById("flt").value;
  shown = ROWS.filter(r => f === "all" || (f === "todo" ? !(r.key in st) : r.set === f));
  document.getElementById("list").innerHTML = shown.map((r, i) => `
    <div class="card${i === cur ? " cur" : ""}" id="c${i}">
      <div class="meta">${r.set} · ${esc(r.brief_id)} · ${r.bucket} · ${esc(r.cite)}</div>
      <div class="brief">${esc(r.brief)}</div><pre>${esc(r.text)}</pre>
      <div class="prop">Sonnet proposes <b class="${r.proposal ? "y" : "n"}">${r.proposal ? "useful" : "not useful"}</b> — ${esc(r.why)}</div>
      <button class="y${st[r.key] === true ? " on" : ""}" onclick="mark(${i},true)">Useful (Y)</button>
      <button class="n${st[r.key] === false ? " on" : ""}" onclick="mark(${i},false)">Not useful (N)</button>
    </div>`).join("");
  const done = ROWS.filter(r => r.key in st).length;
  document.getElementById("prog").textContent = `${done} / ${ROWS.length} confirmed`;
}
function mark(i, v) { st[shown[i].key] = v; save(); cur = Math.min(i + 1, shown.length - 1); render(); go(); }
function go() { const el = document.getElementById("c" + cur); if (el) el.scrollIntoView({block: "center"}); }
document.getElementById("flt").onchange = () => { cur = 0; render(); };
document.addEventListener("keydown", e => {
  if (e.target.tagName === "SELECT") return;
  if (e.key === "y") mark(cur, true); else if (e.key === "n") mark(cur, false);
  else if (e.key === "j") { cur = Math.min(cur + 1, shown.length - 1); render(); go(); }
  else if (e.key === "k") { cur = Math.max(cur - 1, 0); render(); go(); }
});
document.getElementById("exp").onclick = () => {
  const out = ROWS.filter(r => r.key in st).map(r => ({set: r.set, brief_id: r.brief_id, bucket: r.bucket,
    cite: r.cite, useful: st[r.key], proposal: r.proposal}));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], {type: "application/json"}));
  a.download = "confirmed_labels.json"; a.click();
};
render();
</script></body></html>"""


def main() -> None:
    """Build the page from both sets and write it into the git-ignored client folder."""
    rows = _rows(LABELS / "client", "client") + _rows(LABELS, "ipa")
    if not rows:
        sys.exit("labelpage: no pre-labelled pairs found; run labelset.py first")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    OUT.write_text(PAGE.replace("__ROWS__", data), encoding="utf-8")
    print(f"{len(rows)} cards -> {OUT}")


if __name__ == "__main__":
    main()
