"""Short ids for what the model reads and writes back.

The document's ids are random and 12 characters long (f_ZNUOPB5X7JJK). A report reply repeats them about 290
times and a synthesis reply about 160 times: 15-17% of the characters the model writes, paid at the output price,
and a miscopied id is a claim the cite rule drops. Before a call the ids in the payload become aliases that keep
their prefix (f_1, fi_2, ct_3, gap_4), so a prompt that says "pin ids (f_...)" stays true; after it every alias in
the reply goes back to its id. Code never sees an alias: the rules run on the real ids.

Only exact strings are swapped in the payload and in the reply's lists and fields, and in HTML only inside the
`ref` and `refs` attribute values, so prose that happens to contain "f_1" is left alone.
"""

from __future__ import annotations

import re

PREFIXES = ("fi_", "f_", "ct_", "gap_")     # fi_ before f_: the longer prefix wins
_ATTR = re.compile(r"""\b(refs?)=(['"])(.*?)\2""", re.S)


class Aliases:
    def __init__(self, ids):
        self.to_short: dict[str, str] = {}
        counts: dict[str, int] = {}
        for i in dict.fromkeys(x for x in ids if isinstance(x, str)):
            p = next((p for p in PREFIXES if i.startswith(p)), None)
            if p is None:
                continue
            counts[p] = counts.get(p, 0) + 1
            self.to_short[i] = f"{p}{counts[p]}"
        self.to_long = {v: k for k, v in self.to_short.items()}

    def short(self, ids) -> list[str]:
        return [self.to_short.get(i, i) for i in ids]

    def encode(self, obj):
        """The payload with every id that is a whole string replaced by its alias."""
        return _walk(obj, self.to_short, html=False)

    def decode(self, obj):
        """The reply with every alias put back: whole strings, and the tokens of ref/refs attributes in HTML."""
        return _walk(obj, self.to_long, html=True)


def _walk(obj, table: dict, html: bool):
    if isinstance(obj, dict):
        return {k: _walk(v, table, html) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, table, html) for v in obj]
    if isinstance(obj, str):
        if obj in table:
            return table[obj]
        if html and "ref" in obj and "<" in obj:
            return _ATTR.sub(lambda m: f"{m.group(1)}={m.group(2)}"
                                       f"{' '.join(table.get(t, t) for t in m.group(3).split())}{m.group(2)}", obj)
    return obj
