"""Short ids for the model: aliases keep their prefix, go back exactly, and prose is never rewritten."""
import json

from napkin.aliases import Aliases


def test_aliases_keep_their_prefix_and_round_trip():
    al = Aliases(["f_ZNUOPB5X7JJK", "f_ABCDEFGHIJKL", "fi_K5MHYDUWHSPX", "ct_q1w2e3r4", "gap_x9y8", "campaign.problem"])
    assert al.short(["f_ZNUOPB5X7JJK", "fi_K5MHYDUWHSPX", "gap_x9y8", "campaign.problem"]) == \
        ["f_1", "fi_1", "gap_1", "campaign.problem"]
    payload = {"pins": [{"id": "f_ZNUOPB5X7JJK", "value": 0.4}], "findings": [{"id": "fi_K5MHYDUWHSPX"}]}
    assert al.decode(al.encode(payload)) == payload
    assert "ZNUOPB5X7JJK" not in json.dumps(al.encode(payload))


def test_html_refs_go_back_but_prose_does_not_change():
    al = Aliases(["f_ZNUOPB5X7JJK", "f_ABCDEFGHIJKL", "gap_x9y8"])
    html = ("<p>Rule f_1 in prose stays. <clan-cite refs='f_1 f_2'></clan-cite></p>"
            "<clan-chart kind=\"bar\" refs=\"f_2 f_1\" labels=\"A,B\"></clan-chart><clan-gap ref='gap_1'></clan-gap>")
    out = al.decode({"html": html, "cites": ["f_2"], "text": "f_1"})
    assert out["cites"] == ["f_ABCDEFGHIJKL"] and out["text"] == "f_ZNUOPB5X7JJK"
    assert "refs='f_ZNUOPB5X7JJK f_ABCDEFGHIJKL'" in out["html"]
    assert 'refs="f_ABCDEFGHIJKL f_ZNUOPB5X7JJK"' in out["html"] and "ref='gap_x9y8'" in out["html"]
    assert "Rule f_1 in prose stays." in out["html"]
