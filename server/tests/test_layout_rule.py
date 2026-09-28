"""The layout rule on its own: what an agent-written layout may keep."""

from napkin.rules.layout import check

PINS = {"f_01AAAAAA": {"value": 0.22, "unit": "proportion"}, "f_01BBBBBB": {"value": "Q2 2027", "unit": "text"}}
FINDINGS = {"fi_01CCCCCC": {"statement": "Discounters are the door", "status": "proposed"},
            "fi_01DDDDDD": {"statement": "Rejected one", "status": "rejected"}}


def run(html):
    return check(html, PINS, FINDINGS, {"ct_share": {"status": "open"}}, {"gap_rr": {}})


def test_a_figure_in_prose_must_be_held_by_what_the_paragraph_references():
    out, dropped, _ = run('<p>They take 22% of sales. <clan-cite refs="f_01AAAAAA"></clan-cite></p>'
                          '<p>They take 40% of sales. <clan-cite refs="f_01AAAAAA"></clan-cite></p>'
                          '<p>Since 2019 it grew. <clan-cite refs="f_01AAAAAA"></clan-cite></p>')
    assert "22%" in out and "40%" not in out and "Since 2019" in out
    assert any("states 40" in d for d in dropped)


def test_refs_must_name_what_the_document_holds():
    out, dropped, counts = run('<p><clan-field ref="fi_01CCCCCC" as="claim"></clan-field></p>'
                               '<p><clan-field ref="fi_01DDDDDD" as="claim"></clan-field> x</p>'
                               '<p><clan-field ref="ct_share"></clan-field> y</p>'
                               '<clan-chart kind="line" refs="f_01AAAAAA f_01BBBBBB"></clan-chart>'
                               '<clan-field ref="f_01AAAAAA" as="huge"></clan-field>')
    assert 'ref="fi_01CCCCCC"' in out and "fi_01DDDDDD" not in out and 'ref="ct_share"' in out
    assert "<clan-chart" not in out, "a chart over a text pin is dropped"
    assert "huge" not in out
    assert counts["fields"] == 2


def test_nothing_outside_the_vocabulary_survives():
    out, _, _ = run('<div class="cl-band bad" style="color:red" onmouseover="x()" data-tone="soft">'
                    '<p>ok <clan-cite refs="f_01AAAAAA"></clan-cite></p><iframe src="//e"><p>inner</p></iframe>'
                    '<style>*{}</style><svg><script>1</script></svg></div><clan-field ref="f_01AAAAAA">text inside</clan-field>')
    assert out.startswith('<div class="cl-band" data-tone="soft"><p>ok')
    for gone in ("bad", "style", "onmouseover", "iframe", "inner", "svg", "script", "text inside"):
        assert gone not in out, gone
