"""copy.py: the local draft lands in an empty database whole, as it was, through
the target's own rules, or not at all."""

from __future__ import annotations

import psycopg
import pytest

from conftest import uri
from napkin_layers.copy import DATA, CopyRefused, copy, digest
from test_layers_schema import Y2025

SRC, DST = "napkin_category_copy_src", "napkin_category_copy_dst"


def conn(pg, db):
    return psycopg.connect(uri(pg, db, f"{db}_app"))


def write_source(session):
    with session(SRC, org="org/napkin") as s:
        a = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        s.append(440_000_000, quote="€440 million in 2025",
                 excerpt=s.capture(uri="https://other.ie/r", text="Worth €440 million in 2025.", publisher="Other"),
                 period=Y2025)                                    # same period, another value: a contest
        s.append(12, quote="12 brands", excerpt=s.capture(uri="https://u.ie/x", text="There are 12 brands."),
                 key="market.player_count", unit="count")         # undated, alone: active
        s.conn.commit()
    return a


def test_the_draft_lands_whole_and_identical_and_only_once(pg, session):
    write_source(session)
    with conn(pg, SRC) as src, conn(pg, DST) as dst:
        counts = copy(src, dst, "org/napkin", log=lambda *_: None)
    assert counts["facts"] == 3 and counts["evidence"] == 3
    with conn(pg, SRC) as src, conn(pg, DST) as dst:
        for c in (src, dst):
            c.execute("SELECT set_config('napkin.org', 'org/napkin', true)")
        for t in DATA:
            assert digest(src, t) == digest(dst, t), t
        statuses = dst.execute("SELECT status, count(*) FROM layers.facts GROUP BY 1 ORDER BY 1").fetchall()
    assert dict(statuses) == {"contested": 2, "active": 1}

    # a second copy is refused and changes nothing
    with conn(pg, SRC) as src, conn(pg, DST) as dst:
        with pytest.raises(CopyRefused, match="must be empty"):
            copy(src, dst, "org/napkin", log=lambda *_: None)
    with conn(pg, DST) as dst:
        dst.execute("SELECT set_config('napkin.org', 'org/napkin', true)")
        assert dst.execute("SELECT count(*) FROM layers.facts").fetchone()[0] == 3
