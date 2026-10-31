"""The team's facts as a graph: the schema decides what may be added; every edge keeps its evidence."""

from meeseek.context import ontology

ROSTER = """# Team Members
## Acme App Team
- Grace
- Heidi
## 原創團隊
- Ivan
### 遊戲開發
- Judy
- 王小明 (Ming)
"""


def test_the_roster_comes_in_confirmed_with_nested_teams():
    g = ontology.import_roster(ontology.empty(), ROSTER)
    s = ontology.summary(g)
    assert "- Grace 屬於 Acme App Team（確認）" in s
    assert "- 遊戲開發 隸屬 原創團隊（確認）" in s
    assert "- Judy 屬於 遊戲開發（確認）" in s
    assert "- 王小明 屬於 遊戲開發（確認）" in s          # "(Ming)" is an alias, not part of the name


def test_the_schema_keeps_out_what_does_not_fit():
    g = ontology.empty()
    assert ontology.add(g, ("人", "Alice"), "回報給", ("人", "Nina"), source="SDK 討論 2026-04-23", quote="要跟 Nina 確認")
    assert not ontology.add(g, ("人", "Alice"), "回報給", ("團隊", "Sotatek"), source="x")   # range is a person
    assert not ontology.add(g, ("團隊", "Sotatek"), "擔任", ("角色", "SDK"), source="x")     # domain is a person
    assert not ontology.add(g, ("人", "Bob"), "是主管", ("人", "Grace"), source="x")          # no such relation
    assert not ontology.add(g, ("人", ""), "屬於", ("團隊", "X"), source="x")
    assert len(g["edges"]) == 1


def test_an_edge_counts_each_meeting_once_and_is_a_guess_until_seen_twice():
    g = ontology.empty()
    for source in ("例會 2026-06-23", "例會 2026-06-23", "例會 2026-06-30"):
        ontology.add(g, ("人", "Heidi"), "分派給", ("人", "Rupert"), source=source, quote="Rupert 你幫忙看")
    ontology.add(g, ("人", "Quinn"), "負責", ("領域", "對話顯示"), source="PRD 2026-04-30", quote="顯示交給 Quinn")
    s = ontology.summary(g).splitlines()
    assert s == ["- Heidi 分派給 Rupert（2 場會）", "- Quinn 負責 對話顯示（1 場會，推斷）"]


def test_the_page_draws_the_graph_and_lists_the_evidence():
    g = ontology.import_roster(ontology.empty(), ROSTER)
    ontology.add(g, ("人", "Heidi"), "分派給", ("人", "Grace"), source="例會 2026-06-30", quote="Grace 你帶大家過文件")
    ontology.save(g)
    page = ontology.page_file().read_text()
    assert "```mermaid" in page and "flowchart LR" in page
    assert 'subgraph n0["Acme App Team"]' in page and 'n1("Grace")' in page   # a person drawn inside the team
    assert "-.->|分派給|" in page                                          # a guess dotted
    assert "「Grace 你帶大家過文件」" in page
    assert ontology.load()["edges"]


def test_a_name_is_one_node_whatever_class_a_meeting_gives_it():
    g = ontology.import_roster(ontology.empty(), "## Sotatek\n- Alice\n")
    # a meeting calls Sotatek a company: same node, same edge, now also seen in that meeting
    assert ontology.add(g, ("人", "Alice"), "屬於", ("公司", "Sotatek"), source="SDK 討論 2026-04-23")
    assert ontology.summary(g).splitlines() == ["- Alice 屬於 Sotatek（確認）"]
    assert g["edges"][0]["sources"] == ["團隊名單", "SDK 討論 2026-04-23"]
    # a person may not take a role the graph says is a team
    assert not ontology.add(g, ("人", "Bob"), "擔任", ("角色", "Sotatek"), source="x")


def test_an_older_graph_is_compacted_by_name():
    old = {"nodes": {"人:alice": {"class": "人", "name": "Alice"}, "團隊:sotatek": {"class": "團隊", "name": "Sotatek"},
                     "公司:sotatek": {"class": "公司", "name": "Sotatek"}},
           "edges": [{"s": "人:alice", "rel": "屬於", "o": "團隊:sotatek", "sources": ["團隊名單"], "evidence": []},
                     {"s": "人:alice", "rel": "屬於", "o": "公司:sotatek", "sources": ["m1", "m2"], "evidence": ["q"]}]}
    g = ontology.compact(old)
    assert len(g["nodes"]) == 2 and len(g["edges"]) == 1
    assert g["edges"][0]["sources"] == ["團隊名單", "m1", "m2"]


def test_the_roster_relates_teams_lists_who_left_and_replaces_what_it_confirmed_before():
    g = ontology.import_roster(ontology.empty(), ROSTER + "## 已離開\n- Peggy (已不在團隊)\n")
    ontology.add(g, ("人", "Heidi"), "分派給", ("人", "Grace"), source="例會 2026-06-30")
    assert not ontology.add(g, ("人", "Peggy"), "屬於", ("團隊", "Acme App Team"), source="例會 2026-06-23")  # left
    new = """## Acme App Team
- Grace
- Heidi
- Trent
## 三叉團隊 (原創的外包)
### 遊戲開發
- Judy
## 團隊之間
- Acme App Team 合作 原創團隊
- 原創團隊 發包給 三叉團隊
## 已離開
- Sybil Wu (離職)
"""
    g = ontology.import_roster(g, new)
    s = ontology.summary(g)
    assert "Trent 屬於 Acme App Team（確認）" in s
    assert "遊戲開發 隸屬 三叉團隊（確認）" in s and "隸屬 原創團隊" not in s     # the old roster's nesting is gone
    assert "Acme App Team 合作 原創團隊（確認）" in s and "原創團隊 發包給 三叉團隊（確認）" in s
    assert "Ivan" not in s                                                     # no longer in the roster
    assert "Heidi 分派給 Grace（1 場會，推斷）" in s                              # what meetings showed stays
    assert ontology.gone(g, "Sybil Wu") and not ontology.gone(g, "Peggy")
    assert "Sotatek" == ontology._name(g, ontology.node(g, "公司", "Sotatek (Relay Chat SDK 外包)"))


def test_the_roster_outranks_an_inferred_team_for_someone_it_places():
    g = ontology.import_roster(ontology.empty(), ROSTER)
    ontology.add(g, ("人", "Ivan"), "屬於", ("團隊", "遊戲開發"), source="例會 2026-06-09")     # roster says 原創團隊
    ontology.add(g, ("人", "林大華"), "屬於", ("團隊", "遊戲開發"), source="例會 2026-06-09")    # not in the roster
    s = ontology.summary(g)
    assert "Ivan 屬於 遊戲開發" not in s and "林大華 屬於 遊戲開發（1 場會，推斷）" in s


def test_an_edge_the_roster_says_does_not_hold_is_gone_and_stays_gone():
    g = ontology.empty()
    ontology.add(g, ("人", "Oscar Lin"), "分派給", ("人", "Grace"), source="例會 2026-05-12")
    g = ontology.import_roster(g, ROSTER + "## 不成立\n- Oscar Lin 分派給 Grace\n")
    assert "分派給" not in ontology.summary(g)
    assert not ontology.add(g, ("人", "Oscar Lin"), "分派給", ("人", "Grace"), source="例會 2026-06-02")


def test_a_roster_alias_is_the_same_person_and_merges_what_was_made_under_it():
    """2026-10-21: 崇安 (how the transcripts heard 崇恩) was a second Wayne,
    and the graph had Wayne assigning work to himself."""
    g = ontology.empty()
    ontology.add(g, ("人", "崇安"), "負責", ("領域", "知識庫"), source="例會 2026-10-20", quote="崇安負責知識庫")
    ontology.add(g, ("人", "Wayne"), "分派給", ("人", "崇安"), source="例會 2026-10-20", quote="崇安你看")
    g = ontology.import_roster(g, "## Acme App Team\n- Wayne (崇恩、崇安)\n- Kathy (yupingwang)\n")
    s = ontology.summary(g)
    assert "崇安" not in s and "- Wayne 負責 知識庫" in s and "分派給" not in s     # merged; the self-edge gone
    ontology.add(g, ("人", "yupingwang"), "負責", ("領域", "客服自動化"), source="例會 2026-10-21", quote="yupingwang 負責")
    assert "- Kathy 負責 客服自動化" in ontology.summary(g)


def test_a_guess_retires_after_a_month_and_a_pattern_once_it_stops_turning_up():
    day = 86400
    g = ontology.empty()
    ontology.add(g, ("人", "Heidi"), "分派給", ("人", "Rupert"), source="例會 2026-06-23")
    ontology.add(g, ("人", "Ivan"), "回報給", ("人", "Judy"), source="例會 2026-06-23")
    ontology.add(g, ("人", "Ivan"), "回報給", ("人", "Judy"), source="例會 2026-06-30")
    g = ontology.import_roster(g, "## Acme App Team\n- Grace\n")
    seen = g["edges"][0]["seen"]
    kept = ontology.prune(g, now=seen + 31 * day)
    assert [e["rel"] for e in kept["edges"] if e["s"] != "grace"] == ["回報給"]       # the guess went, the pattern stays
    assert "Grace 屬於 Acme App Team" in ontology.summary(ontology.prune(g, now=seen + 400 * day))   # the roster never goes
    assert all(e["s"] == "grace" for e in ontology.prune(g, now=seen + 181 * day)["edges"])


def test_decisions_are_the_notes_business_not_the_graph():
    g = ontology.empty()
    assert not ontology.add(g, ("人", "Quinn"), "決定", ("領域", "交付時程"), source="例會 2026-10-20")


def test_only_patterns_beyond_the_roster_reach_every_agent():
    g = ontology.import_roster(ontology.empty(), ROSTER)
    for src in ("例會 2026-10-13", "例會 2026-10-20"):
        ontology.add(g, ("人", "Heidi"), "分派給", ("人", "Grace"), source=src, quote="Grace 你看")
    ontology.add(g, ("人", "Ivan"), "回報給", ("人", "Judy"), source="例會 2026-10-20")
    ontology.save(g)
    p = ontology.patterns()
    assert p == "- Heidi 分派給 Grace（2 場會）"                                     # not the roster, not a guess
