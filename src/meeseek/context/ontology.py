"""The team's facts as a graph: typed nodes, typed edges, each edge with its evidence.

    classes     人 (Person) · 團隊 (Team) · 公司 (Company) · 角色 (Role) · 領域 (Area)
    relations   屬於     人 → 團隊 | 公司
                隸屬     團隊 → 團隊 | 公司
                發包給   團隊 | 公司 → 團隊 | 公司
                合作     團隊 | 公司 → 團隊 | 公司
                擔任     人 → 角色
                負責     人 | 團隊 → 領域
                回報給   人 → 人
                分派給   人 → 人

Every edge carries where it came from. The team roster
(notes/pinned/team_members.md, `import_roster`) is the authority and gives
confirmed edges; it also lists who left (kept out of the picture and summary,
never inferred again), edges that do not hold (「不成立」), and each person's
other names — "Wayne (崇恩、崇安)": a meeting that says 崇恩 means Wayne, and
nodes made under an alias merge into his. A meeting (`learn`) gives inferred
edges, each with the sentence it rests on and the meetings it was seen in.
Code holds the schema: what the model reads out of a meeting is kept only when
its types fit the relation (`add`). An edge lives by context/lifecycle.py:
seen once, a guess retired after a month unless seen again; in several
meetings, a pattern retired once it stops turning up — each save drops what
has run out, so the graph stays the team as it is.

The graph lives in .cache/ontology.json under the home; notes/ontology.md is rendered from
it (a Mermaid picture plus every edge's evidence). `summary()` is what the
meeting prompts get (the rehearsal, the coach's report, the transcription);
`patterns()` — what meetings showed more than once, beyond the roster every
agent already has — goes into every agent's memory.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import lifecycle, prompts

logger = logging.getLogger(__name__)


def store_file() -> Path:
    return home().data / "ontology.json"


def page_file() -> Path:
    return home().notes / "ontology.md"


def roster_file() -> Path:
    return home().notes / "pinned" / "team_members.md"


CLASSES = ("人", "團隊", "公司", "角色", "領域")
RELATIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {   # name: (domain, range)
    "屬於": (("人",), ("團隊", "公司")),
    "隸屬": (("團隊",), ("團隊", "公司")),
    "發包給": (("團隊", "公司"), ("團隊", "公司")),
    "合作": (("團隊", "公司"), ("團隊", "公司")),
    "擔任": (("人",), ("角色",)),
    "負責": (("人", "團隊"), ("領域",)),
    "回報給": (("人",), ("人",)),
    "分派給": (("人",), ("人",)),
}
CONFIRMED = "團隊名單"
_SURE = lifecycle.SURE   # seen in this many meetings, an inferred edge is a pattern rather than a guess
_KEEP_EVIDENCE = 4
PATTERNS_CHARS = 800     # what of the graph goes into every agent's memory
_ALIAS_SPLIT = re.compile(r"[、,，/／]")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _key(name: str) -> str:
    """A node is its name, without a note in brackets: "Sotatek" and "Sotatek
    (Relay Chat SDK 外包)" are one node, whether a meeting calls it a team or a
    company."""
    return _norm(_bare(name)[0])


def _id(g: dict, name: str) -> str:
    """The node a name stands for: its own key, or the person it is an alias of."""
    k = _key(name)
    return g.get("aliases", {}).get(k, k)


# --- the graph -----------------------------------------------------------------------

def empty() -> dict:
    return {"nodes": {}, "edges": []}


def load() -> dict:
    try:
        g = json.loads(store_file().read_text(encoding="utf-8"))
        return compact(g) if "nodes" in g else empty()
    except (OSError, ValueError):
        return empty()


def compact(g: dict) -> dict:
    """Nodes keyed by name (an alias's under the person's), edges between the
    same nodes merged, an edge from a node to itself dropped (what a missing
    alias made: "Wayne 分派給 崇安"), edges the schema no longer allows
    dropped, and nodes left with no edge."""
    ids = {old: _id(g, n["name"]) for old, n in g["nodes"].items()}
    aliases = g.get("aliases", {})
    nodes: dict = {}
    for old, n in g["nodes"].items():
        if ids[old] not in nodes or old == ids[old]:   # the person's own node wins over an alias's
            nodes[ids[old]] = n
    edges: dict = {}
    for e in g["edges"]:
        s_, o_ = ids.get(e["s"]) or aliases.get(e["s"], e["s"]), ids.get(e["o"]) or aliases.get(e["o"], e["o"])
        if s_ == o_:
            continue
        k = (s_, e["rel"], o_)
        if k in edges:
            m = edges[k]
            m["sources"] += [x for x in e["sources"] if x not in m["sources"]]
            m["evidence"] = (m["evidence"] + [x for x in e["evidence"] if x not in m["evidence"]])[-_KEEP_EVIDENCE:]
            m["seen"] = max(m.get("seen", 0), e.get("seen", 0))
        else:
            edges[k] = {**e, "s": k[0], "o": k[2]}

    def fits(e: dict) -> bool:
        spec = RELATIONS.get(e["rel"])
        return bool(spec) and nodes.get(e["s"], {}).get("class") in spec[0] \
            and nodes.get(e["o"], {}).get("class") in spec[1]
    kept = [e for e in edges.values() if fits(e)]
    used = {e["s"] for e in kept} | {e["o"] for e in kept}
    return {**{k: v for k, v in g.items() if k not in ("nodes", "edges")},
            "nodes": {k: n for k, n in nodes.items() if k in used or n.get("left")}, "edges": kept}


def _last_seen(e: dict) -> float:
    return lifecycle.last_seen_of([s for s in e["sources"] if s != CONFIRMED], e.get("seen", 0))


def prune(g: dict, now: float | None = None) -> dict:
    """Without the inferred edges whose time has run out (context/lifecycle.py)."""
    g["edges"] = [e for e in g["edges"]
                  if not lifecycle.expired(confirmed=confirmed(e), seen=_meetings(e), last_seen=_last_seen(e), now=now)]
    return compact(g)


def node(g: dict, cls: str, name: str) -> str:
    """The node called `name`, made with class `cls` if new; its id. An
    existing node keeps the class it was made with."""
    k = _id(g, name)
    g["nodes"].setdefault(k, {"class": cls, "name": _bare(name)[0]})
    return k


def _class(g: dict, cls: str, name: str) -> str:
    """The class `name` has in the graph, or `cls` when it is not in it yet."""
    return g["nodes"].get(_id(g, name), {}).get("class", cls)


def add(g: dict, s: tuple[str, str], rel: str, o: tuple[str, str], *, source: str, quote: str = "") -> bool:
    """Add the edge s —rel→ o seen in `source`; False when it does not fit the
    schema (an unknown relation, or a node of the wrong class for it — the
    class a node already has counts, not the one proposed)."""
    spec = RELATIONS.get(rel)
    if spec is None or not s[1].strip() or not o[1].strip():
        return False
    if _class(g, *s) not in spec[0] or _class(g, *o) not in spec[1]:
        return False
    if gone(g, s[1]) or gone(g, o[1]) or [_id(g, s[1]), rel, _id(g, o[1])] in g.get("rejected", []):
        return False
    if _id(g, s[1]) == _id(g, o[1]):
        return False                     # one person under two names
    sid, oid = node(g, *s), node(g, *o)
    e = next((e for e in g["edges"] if (e["s"], e["rel"], e["o"]) == (sid, rel, oid)), None)
    if e is None:
        e = {"s": sid, "rel": rel, "o": oid, "sources": [], "evidence": []}
        g["edges"].append(e)
    if source not in e["sources"]:
        e["sources"].append(source)
        if source != CONFIRMED:
            e["seen"] = time.time()
        if quote.strip():
            e["evidence"] = (e["evidence"] + [f"{source}：「{quote.strip()}」"])[-_KEEP_EVIDENCE:]
    return True


def gone(g: dict, name: str) -> bool:
    """Whether `name` has left (the roster's 「已離開」)."""
    return bool(g["nodes"].get(_id(g, name), {}).get("left"))


def _current(g: dict) -> list[dict]:
    """The edges that hold now: none touching anyone who left, none the roster
    says do not hold (「不成立」), and no inferred 屬於 for someone whose team
    the roster confirms — on membership the roster is the authority."""
    left = {k for k, n in g["nodes"].items() if n.get("left")}
    placed = {e["s"] for e in g["edges"] if e["rel"] == "屬於" and CONFIRMED in e["sources"]}
    rejected = [tuple(r) for r in g.get("rejected", [])]
    return [e for e in g["edges"] if e["s"] not in left and e["o"] not in left
            and (e["s"], e["rel"], e["o"]) not in rejected
            and not (e["rel"] == "屬於" and e["s"] in placed and CONFIRMED not in e["sources"])]


def confirmed(e: dict) -> bool:
    return CONFIRMED in e["sources"]


def _meetings(e: dict) -> int:
    return len([s for s in e["sources"] if s != CONFIRMED])


def _label(e: dict) -> str:
    if confirmed(e):
        return "確認"
    n = _meetings(e)
    return f"{n} 場會" + ("" if n >= _SURE else "，推斷")


def save(g: dict) -> None:
    g = prune(g)
    store_file().parent.mkdir(parents=True, exist_ok=True)
    store_file().write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")
    page_file().parent.mkdir(parents=True, exist_ok=True)
    page_file().write_text(render(g), encoding="utf-8")


# --- reading it out ---------------------------------------------------------------------

def _name(g: dict, nid: str) -> str:
    return g["nodes"].get(nid, {}).get("name", nid)


def _ordered(g: dict) -> list[dict]:
    return sorted(_current(g), key=lambda e: (not confirmed(e), -_meetings(e), _name(g, e["s"]), e["rel"]))


def summary(g: dict | None = None) -> str:
    """One line per edge — confirmed first, then the most seen: what prompts get."""
    g = prune(import_roster(load())) if g is None else g
    return "\n".join(f"- {_name(g, e['s'])} {e['rel']} {_name(g, e['o'])}（{_label(e)}）" for e in _ordered(g))


def patterns(limit: int = PATTERNS_CHARS) -> str:
    """What meetings showed more than once and the roster does not say — the
    part of the graph every agent's memory gets (the roster is pinned there
    already), within `limit` characters."""
    g = prune(import_roster(load()))
    out, size = [], 0
    for e in _ordered(g):
        if confirmed(e) or _meetings(e) < _SURE:
            continue
        line = f"- {_name(g, e['s'])} {e['rel']} {_name(g, e['o'])}（{_meetings(e)} 場會）"
        if size + len(line) > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def tidy() -> int:
    """The nightly pass: the roster read again and what has run out dropped;
    how many edges went."""
    g = import_roster(load())
    before = len(g["edges"])
    save(g)
    return before - len(load()["edges"])


def mermaid(g: dict) -> str:
    """The graph as a picture: each person inside their team's box (the most
    certain 屬於), teams boxed inside the team they are part of, every other
    relation an arrow — solid when confirmed or seen often, dotted a guess."""
    ids = {nid: f"n{i}" for i, nid in enumerate(g["nodes"])}
    sure = lambda e: confirmed(e) or _meetings(e) >= _SURE  # noqa: E731
    home: dict[str, str] = {}            # node -> the box it is drawn in
    for e in _ordered(g):                # most certain first
        if e["rel"] in ("屬於", "隸屬") and e["s"] not in home \
                and g["nodes"].get(e["o"], {}).get("class") in ("團隊", "公司"):
            home[e["s"]] = e["o"]
    boxes = {nid for nid, n in g["nodes"].items() if n["class"] in ("團隊", "公司")}
    shape = {"人": ('("', '")'), "角色": ('{{"', '"}}'), "領域": ('>"', '"]')}
    lines = ["flowchart LR"]

    def draw(box: str | None, depth: int) -> None:
        pad = "  " * depth
        for nid, n in g["nodes"].items():
            if home.get(nid) != box or nid == box or n.get("left"):
                continue
            if nid in boxes:
                lines.append(f'{pad}subgraph {ids[nid]}["{n["name"]}"]')
                draw(nid, depth + 1)
                lines.append(f"{pad}end")
            else:
                a_, b_ = shape.get(n["class"], ('["', '"]'))
                lines.append(f'{pad}{ids[nid]}{a_}{n["name"]}{b_}')

    draw(None, 1)
    drawn_in = {(s_, o) for s_, o in home.items()}
    for e in _ordered(g):
        if (e["s"], e["o"]) in drawn_in:
            continue                     # shown by the box it sits in
        lines.append(f'  {ids[e["s"]]} {"-->" if sure(e) else "-.->"}|{e["rel"]}| {ids[e["o"]]}')
    return "\n".join(lines)


def render(g: dict) -> str:
    out = ["# 團隊關係圖（ontology）", "",
           "meeseek 維護的團隊事實圖：團隊名單匯入的邊標「確認」，從會議推斷的邊附依據與看到的會議數，"
           "只看過一次的標「推斷」。這份會被覆寫；要更正或確認，改 `pinned/team_members.md`。", "",
           "```mermaid", mermaid(g), "```", "", "## 每條關係的依據", ""]
    for e in _ordered(g):
        out.append(f"- **{_name(g, e['s'])}** {e['rel']} **{_name(g, e['o'])}**（{_label(e)}）")
        out += [f"  - {ev}" for ev in e["evidence"]]
    left = [n for n in g["nodes"].values() if n.get("left")]
    if left:
        out += ["", "## 已離開", "", "不畫在圖上，也不再從會議推斷他們的關係。", ""]
        out += [f"- {n['name']}" + (f"（{n['left']}）" if isinstance(n["left"], str) and n["left"] else "") for n in left]
    return "\n".join(out) + "\n"


# --- what it is built from ---------------------------------------------------------------

def _bare(name: str) -> tuple[str, str]:
    """A roster name and its note: "王小明 (Ming)" → ("王小明", "Ming")."""
    m = re.match(r"^(.*?)\s*[（(]([^）)]*)[）)]\s*$", name.strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else (name.strip(), "")


def import_roster(g: dict, text: str | None = None) -> dict:
    """The roster's headings are teams (a ### team is part of the ## above it),
    its bullets the people in them; under 「團隊之間」 each bullet is an edge
    ("營運 發包給 原創團隊"), under 「已離開」 the people who left, and under
    「不成立」 edges that do not hold, whatever a meeting suggests ("Oscar Lin
    分派給 Grace"). All confirmed; it replaces what an earlier roster confirmed."""
    text = roster_file().read_text(encoding="utf-8") if text is None and roster_file().exists() else (text or "")
    # The roster is the authority on what is confirmed: what an older roster
    # confirmed is unconfirmed first, and an edge it alone held goes.
    for e in g["edges"]:
        e["sources"] = [x for x in e["sources"] if x != CONFIRMED]
    g["edges"] = [e for e in g["edges"] if e["sources"]]
    for n in g["nodes"].values():
        n.pop("left", None)
    g["rejected"] = []
    g["aliases"] = {}
    top = sub = None
    mode = "team"
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            heading = _bare(line[3:])[0]
            mode = {"團隊之間": "edges", "已離開": "left", "不成立": "rejected"}.get(heading, "team")
            top, sub = (heading, None) if mode == "team" else (None, None)
            if top:
                node(g, "團隊", top)
        elif line.startswith("### ") and mode == "team":
            sub = _bare(line[4:])[0]
            if top:
                add(g, ("團隊", sub), "隸屬", ("團隊", top), source=CONFIRMED)
        elif line.startswith("- ") and mode == "left":
            name, note = _bare(line[2:])
            g["nodes"].setdefault(_key(name), {"class": "人", "name": name})["left"] = note or True
        elif line.startswith("- ") and mode == "rejected":
            words = line[2:].split()
            rel = next((w for w in words if w in RELATIONS), None)
            if rel and 0 < words.index(rel) < len(words) - 1:
                i = words.index(rel)
                g["rejected"].append([_key(" ".join(words[:i])), rel, _key(" ".join(words[i + 1:]))])
        elif line.startswith("- ") and mode == "edges":
            words = line[2:].split()
            rel = next((w for w in words if w in RELATIONS), None)
            if rel and 0 < words.index(rel) < len(words) - 1:
                i = words.index(rel)
                a, b = " ".join(words[:i]), " ".join(words[i + 1:])
                add(g, (_class(g, "團隊", a), a), rel, (_class(g, "領域" if rel == "負責" else "團隊", b), b),
                    source=CONFIRMED)
        elif line.startswith("- ") and (sub or top):
            name, other = _bare(line[2:])
            for alias in _ALIAS_SPLIT.split(other):
                if alias.strip() and _key(alias) != _key(name):
                    g["aliases"][_key(alias)] = _key(name)
            add(g, ("人", name), "屬於", ("團隊", sub or top), source=CONFIRMED)
    return compact(g)    # nodes made under an alias merge into the person's


class _Triple(BaseModel):
    subject: str = Field(description="主詞的名字")
    subject_class: str = Field(description="人 / 團隊 / 公司 / 角色 / 領域")
    relation: str = Field(description="屬於 / 隸屬 / 發包給 / 合作 / 擔任 / 負責 / 回報給 / 分派給")
    object: str = Field(description="受詞的名字")
    object_class: str = Field(description="人 / 團隊 / 公司 / 角色 / 領域")
    quote: str = Field(description="依據的原句或事實，60 字內")


class _Found(BaseModel):
    triples: list[_Triple]


def schema_text() -> str:
    rels = "\n".join(f"- {r}：{'／'.join(d)} → {'／'.join(o)}" for r, (d, o) in RELATIONS.items())
    return f"節點類型：{'、'.join(CLASSES)}\n關係（主詞類型 → 受詞類型）：\n{rels}"


def learn(transcript: str, notes: str, *, title: str, date: str, me: str = "") -> int:
    """Read one meeting for who is who and add what fits the schema; how many edges."""
    from meeseek.runtime.oneshot import ask

    g = import_roster(load())
    found = ask(prompts.text("internal/ontology_learn", schema=schema_text(), title=title, date=date,
                             me=me or "（不知道）", known=summary(g) or "（還沒有）",
                             notes=notes[:15000] or "（沒有）", transcript=transcript[:60000]),
                model=cfg.models.workflow, schema=_Found, temperature=0.1, max_output_tokens=8000).triples
    source = f"{title} {date}".strip()
    n = sum(add(g, (t.subject_class, t.subject), t.relation, (t.object_class, t.object), source=source,
                quote=t.quote) for t in found)
    save(g)
    return n
