"""
toon_lite.py — a lenient decoder for the subset of TOON the brief pipeline asks models to write.

TOON (Token-Oriented Object Notation, https://github.com/toon-format/spec) is what CLAN
serialises agent context in. Here it is the OUTPUT format of the two biggest generation
calls (Loop 1 capture and how-to-win), because a list of records costs one header line
instead of repeating every key on every item, which is where JSON spent its tokens.

Why not a library: on 2026-09-23 the official `toon-format` 0.1.0 on PyPI was a stub
(`encode` raises NotImplementedError) and `python-toon` 0.1.3 writes a non-spec header
(`key[2,]{...}`). A model's output also needs a forgiving reader, not a strict one: it
miscounts `[N]`, drops the delimiter marker, puts a stray `|` in a value.

Supported (all a model is asked to produce):

    key: scalar                     null / true / false / numbers / "quoted" / bare text
    key:                            a nested object (more-indented lines follow)
    key[N]: a,b,c                   an inline primitive array
    key[N]:                         a list of `- item` primitives on the lines below
    key[N|]{f1|f2|f3}:              a table: one row per more-indented line; the delimiter
                                    is `|` (or `,` / tab when the header says so)

Leniency, each deliberate:
  * `[N]` is never trusted — rows are read until the indentation ends.
  * A row with MORE cells than fields: the surplus is joined back into the first field
    (tables put the free-text `value`/`point` first, so a stray delimiter lands there).
    A row with FEWER cells is padded with None.
  * A table row the model wrapped onto a new line (so the next line is neither a row nor
    a `key:`) is joined back onto the row above. Measured 2026-09-23: one wrapped row
    at indent 0 made a whole 27-point how_to_win reply unreadable.
  * Code fences around the whole reply are stripped; blank lines are skipped.

decode() raises ToonError on text it cannot read at all; callers treat that as "this
reply failed" and fall back.
"""
from __future__ import annotations

import json
import re

__all__ = ["decode", "ToonError"]

_HEADER = re.compile(r"^(?P<key>[^\s:\[\]{}]+)\s*(?:\[(?P<n>\d*)(?P<delim>[|,\t]?)\])?"
                     r"\s*(?:\{(?P<fields>[^}]*)\})?\s*:\s?(?P<rest>.*)$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")


class ToonError(ValueError):
    """The text is not readable as the TOON subset above."""


def _scalar(s: str):
    """One TOON primitive: null / booleans / numbers / a quoted or bare string."""
    s = s.strip()
    if s == "" or s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if _NUMBER.match(s):
        return float(s) if any(c in s for c in ".eE") else int(s)
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    return s


def _split_row(line: str, delim: str) -> list[str]:
    """Split one row on `delim`, honouring double-quoted cells."""
    cells, buf, quoted, i = [], [], False, 0
    while i < len(line):
        c = line[i]
        if c == '"':
            quoted = not quoted
            buf.append(c)
        elif c == "\\" and quoted and i + 1 < len(line):
            buf.append(line[i:i + 2]); i += 1
        elif c == delim and not quoted:
            cells.append("".join(buf)); buf = []
        else:
            buf.append(c)
        i += 1
    cells.append("".join(buf))
    return cells


def _row(line: str, fields: list[str], delim: str) -> dict:
    """A table row as a dict; surplus cells rejoin the first field, missing ones are None."""
    cells = _split_row(line, delim)
    extra = len(cells) - len(fields)
    if extra > 0:
        cells = [delim.join(cells[:extra + 1])] + cells[extra + 1:]
    cells += [""] * (len(fields) - len(cells))
    return {f: _scalar(c) for f, c in zip(fields, cells)}


def decode(text: str) -> dict:
    """Read TOON text into a dict. Raises ToonError when nothing readable is found."""
    body = re.sub(r"^```[a-zA-Z]*\s*$", "", (text or "").strip(), flags=re.MULTILINE)
    lines = [(len(ln) - len(ln.lstrip(" ")), ln.strip()) for ln in body.splitlines() if ln.strip()]
    if not lines:
        raise ToonError("empty")
    pos = 0

    def block(indent: int) -> dict:
        """Parse sibling keys at exactly `indent` (and their children) into a dict."""
        nonlocal pos
        out: dict = {}
        last_table = None                            # (rows, fields, delim, raw of last row)
        while pos < len(lines) and lines[pos][0] >= indent:
            ind, ln = lines[pos]
            if ind > indent:                         # stray deeper line: skip it
                pos += 1
                continue
            m = _HEADER.match(ln)
            if not m and last_table and last_table[0]:
                rows, fields, delim, raw = last_table   # a wrapped row: join it back on
                raw = f"{raw} {ln}"
                rows[-1] = _row(raw, fields, delim)
                last_table = (rows, fields, delim, raw)
                pos += 1
                continue
            if not m:
                raise ToonError(f"line {pos + 1}: not 'key: value' — {ln[:60]!r}")
            last_table = None
            pos += 1
            key, rest = m["key"], m["rest"].strip()
            is_array = m["n"] is not None or m["delim"] or m["fields"] is not None
            child = lines[pos][0] if pos < len(lines) and lines[pos][0] > ind else None
            if m["fields"] is not None:              # table
                delim = m["delim"] or ("|" if "|" in m["fields"] else ",")
                fields = [f.strip() for f in m["fields"].split(delim) if f.strip()]
                rows, raw = [], ""
                while child is not None and pos < len(lines) and lines[pos][0] >= child:
                    raw = lines[pos][1]
                    rows.append(_row(raw, fields, delim)); pos += 1
                out[key] = rows
                last_table = (rows, fields, delim, raw)
            elif is_array and rest:                  # inline primitive array
                out[key] = [_scalar(c) for c in _split_row(rest, m["delim"] or ",")]
            elif is_array or (child is not None and lines[pos][1].startswith("- ")):
                items = []
                while child is not None and pos < len(lines) and lines[pos][0] >= child:
                    items.append(_scalar(lines[pos][1][2:] if lines[pos][1].startswith("- ")
                                         else lines[pos][1])); pos += 1
                out[key] = items
            elif rest:
                out[key] = _scalar(rest)
            elif child is not None:
                out[key] = block(child)
            else:
                out[key] = None
        return out

    result = block(lines[0][0])
    if not result:
        raise ToonError("no keys")
    return result
