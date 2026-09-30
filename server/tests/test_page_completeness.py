"""The agent decides how the report page shows things, never whether: whatever it leaves off is added by code."""

import re

from napkin.pipeline import layout

PINS = {"f_AAAAAA": {"key": "market.size_eur", "market": "IE", "value": 4, "unit": "count"},
        "f_BBBBBB": {"key": "media.adspend_eur", "market": "IE", "value": 9, "unit": "count"},
        "f_CCCCCC": {"key": "media.tv_share_of_spend", "market": "IE", "value": 0.3, "unit": "proportion"}}
FINDINGS = {"fi_DDDDDD": {"statement": "TV leads", "status": "proposed"}}
CONTESTS = {"ct_EE": {"status": "open", "key": "x", "values": []}}
GAPS = {"gap_FF": {"wanted": "size"}}
REPORT = {"headline": {"text": "H", "cites": ["f_AAAAAA"]}, "summary": [],
          "sections": [{"id": "s_market_structure", "title": "The market", "lens": "market_structure", "blocks": [
                           {"kind": "pins", "fact_ids": ["f_AAAAAA"]}, {"kind": "gap", "gap_id": "gap_FF"}]},
                       {"id": "s_media_spend", "title": "Media", "lens": "media_spend", "blocks": [
                           {"kind": "pins", "fact_ids": ["f_BBBBBB", "f_CCCCCC"]},
                           {"kind": "finding", "finding_id": "fi_DDDDDD"}, {"kind": "contest", "contest_id": "ct_EE"}]}]}


def finish(html):
    return layout.finish(html, REPORT, PINS, FINDINGS, CONTESTS, GAPS)


GOOD = ('<h1 class="cl-title">H</h1><section class="cl-block"><p>Lead. <clan-cite refs="f_AAAAAA f_BBBBBB">'
        '</clan-cite></p><clan-field ref="f_AAAAAA" as="big"></clan-field><clan-field ref="f_BBBBBB" as="big">'
        '</clan-field></section><clan-sources></clan-sources>')


def ids(html):
    return set(re.findall(r"\b(?:f|fi|ct|gap)_[0-9A-Za-z]+\b", html))


def test_a_page_that_shows_everything_is_left_exactly_as_the_agent_wrote_it():
    html = GOOD.replace("</section>", '<p>More. <clan-cite refs="f_CCCCCC"></clan-cite><clan-field ref="fi_DDDDDD" as="claim">'
                        '</clan-field><clan-field ref="ct_EE"></clan-field><clan-gap ref="gap_FF"></clan-gap></p></section>')
    out, by, dropped = finish(html)
    assert by == "agent" and ids(out) == {"f_AAAAAA", "f_BBBBBB", "f_CCCCCC", "fi_DDDDDD", "ct_EE", "gap_FF"}
    assert "Also in the research" not in out


def test_whatever_the_agent_leaves_out_is_added_by_code_under_its_lens():
    out, by, _ = finish(GOOD)
    assert by == "agent"                                   # the agent's page stays; code only adds
    assert ids(out) == {"f_AAAAAA", "f_BBBBBB", "f_CCCCCC", "fi_DDDDDD", "ct_EE", "gap_FF"}
    tail = out[out.index("Also in the research"):]
    assert "fi_DDDDDD" in tail and "ct_EE" in tail and "gap_FF" in tail and "f_CCCCCC" in tail
    assert "f_AAAAAA" not in tail                           # what the page already shows is not repeated
    assert out.rstrip().endswith("<clan-sources></clan-sources></footer>") or "<clan-sources>" in out[-200:]
    assert out.index("Also in the research") < out.index("<clan-sources>")


def test_a_built_page_needs_no_completion():
    out, by, _ = finish(None)
    assert by == "built" and "Also in the research" not in out
    assert ids(out) == {"f_AAAAAA", "f_BBBBBB", "f_CCCCCC", "fi_DDDDDD", "ct_EE", "gap_FF"}
