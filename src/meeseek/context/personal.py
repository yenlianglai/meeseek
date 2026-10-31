"""What meeseek knows about you: a graph of you alone, so every agent works the
way you want without being told again.

The team graph (context/ontology.py) is who is who; this one has one subject,
you, and what holds of you:

    偏好       how you want things done          ("回覆精簡，用繁體中文")
    不要       what you do not want               ("打到 Claude Code 的用量不要算錢")
    習慣       how you work                        ("UI 改動不影響功能就直接改")
    擅長       what you are good at
    正在練習   what a coach report said to work on
    目標       what you are after
    關注       what you keep an eye on

Each fact keeps its sources and your own words for it. It is built from:

  - notes/about_me.md — yours to write, and the authority: a fact under its
    heading is confirmed; under 「不成立」 ("不要 xxx") a fact is dropped and
    not learned again (`import_about_me`);
  - your conversations with meeseek — the console's and what you send from
    your phone — read each night for what you said about how you want things
    (`learn_from_chat`) — a fact said once is a guess, said in two
    conversations a pattern;
  - the coach's report: its 「下次練習」 is what you are practising now
    (`note_practice`), the last report's replacing the one before.

A fact lives by context/lifecycle.py: a guess not heard again in a month and
a pattern no longer heard are dropped at each save and in the nightly tidy,
and a rewording of a fact is that fact seen again, not a new one. It holds
nothing about anyone else. The facts live in .cache/personal.json under the home, and
notes/about_me.md is one file for both sides: the person's part on top, the
authority; meeseek's below its 「推斷」 heading, rewritten on every save with
each fact's count and evidence — confirming one is moving its line up.
`summary()` goes into every agent's memory block (context/memory.py) — what
you are practising stays out of it: it is the coach's business, not the
sprint agent's.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from datetime import datetime

from pydantic import BaseModel, Field

from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import lifecycle, prompts

logger = logging.getLogger(__name__)


def store_file() -> Path:
    return home().data / "personal.json"


def about_me_file() -> Path:
    return home().notes / "about_me.md"


RELATIONS = ("偏好", "不要", "習慣", "擅長", "正在練習", "目標", "關注")
CONFIRMED = "about_me"
_SURE = lifecycle.SURE
_KEEP_EVIDENCE = 3
SUMMARY_CHARS = 1200
PRACTICE = "正在練習"
_PRACTICE = re.compile(r"^## 下次練習\s*\n(.*?)(?=^## |\Z)", re.S | re.M)

ABOUT_ME_TEMPLATE = """# 關於我

meeseek 照這份認識你。上面是你寫的：每個標題下的一行是一件確定的事，所有 agent 每輪都看得到；
不成立的寫在「不成立」底下（例如「- 不要 用表格」），那件事會拿掉、不再學。
最下面「推斷」那段是 meeseek 從你的對話和會後報告推斷的，會整段重寫：確定的就把那一行搬上來。

## 偏好

## 不要

## 習慣

## 目標

## 不成立
"""
# Where meeseek's part of about_me.md begins: from this heading to the end is
# rewritten on every save; everything above it is the person's and is kept as is.
INFERRED = "## 推斷（meeseek 維護，這段會整段重寫）"
_INFERRED_AT = re.compile(r"^## 推斷", re.M)
# What a line carries in the inferred part ("（2 次）", "（推斷）"): dropped
# when the person moves the line up into their part.
_NOTE = re.compile(r"\s*[（(](?:\d+\s*次|推斷|確認)[^）)]*[）)]\s*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def empty() -> dict:
    return {"facts": [], "rejected": []}


def load() -> dict:
    try:
        d = json.loads(store_file().read_text(encoding="utf-8"))
        return d if "facts" in d else empty()
    except (OSError, ValueError):
        return empty()


def add(d: dict, rel: str, what: str, *, source: str, quote: str = "") -> bool:
    """Add "you —rel→ what" seen in `source`; False for an unknown relation, an
    empty fact, or one 「不成立」 rules out."""
    what = what.strip()
    if rel not in RELATIONS or not what or [rel, _norm(what)] in d["rejected"]:
        return False
    f = next((f for f in d["facts"] if f["rel"] == rel and _norm(f["what"]) == _norm(what)), None) \
        or next((f for f in d["facts"] if f["rel"] == rel and lifecycle.same(f["what"], what)), None)
    if f is None:
        f = {"rel": rel, "what": what, "sources": [], "evidence": [], "since": time.time()}
        d["facts"].append(f)
    if source not in f["sources"]:
        f["sources"].append(source)
        f["seen"] = time.time()
        if quote.strip():
            f["evidence"] = (f["evidence"] + [f"{source}：「{quote.strip()}」"])[-_KEEP_EVIDENCE:]
    return True


def confirmed(f: dict) -> bool:
    return CONFIRMED in f["sources"]


def _seen(f: dict) -> int:
    return len([s for s in f["sources"] if s != CONFIRMED])


def _label(f: dict) -> str:
    if confirmed(f):
        return "確認"
    return f"{_seen(f)} 次" + ("" if _seen(f) >= _SURE else "，推斷")


def _ordered(d: dict) -> list[dict]:
    return sorted(d["facts"], key=lambda f: (not confirmed(f), -_seen(f), -f.get("seen", 0)))


def prune(d: dict, now: float | None = None) -> dict:
    """Without the facts whose time has run out (context/lifecycle.py)."""
    d["facts"] = [f for f in d["facts"]
                  if not lifecycle.expired(confirmed=confirmed(f), seen=_seen(f),
                                           last_seen=f.get("seen") or f.get("since", 0), now=now)]
    return d


def tidy() -> int:
    """The nightly pass: about_me.md read again and what has run out dropped;
    how many facts went."""
    d = import_about_me(load())
    before = len(d["facts"])
    save(d)
    return before - len(load()["facts"])


def import_about_me(d: dict, text: str | None = None) -> dict:
    """about_me.md: each heading a relation, each bullet a confirmed fact;
    「不成立」 bullets ("不要 xxx") rule a fact out. Replaces what an earlier
    version confirmed."""
    if text is None:
        text = about_me_file().read_text(encoding="utf-8") if about_me_file().exists() else ""
    text = _yours(text)
    for f in d["facts"]:
        f["sources"] = [s for s in f["sources"] if s != CONFIRMED]
    d["facts"] = [f for f in d["facts"] if f["sources"]]
    d["rejected"] = []
    section = None
    for raw in text.splitlines():
        line = _NOTE.sub("", raw.rstrip())
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("- ") and section == "不成立":      # a top-level bullet; an indented one is evidence
            words = line[2:].split(maxsplit=1)
            if len(words) == 2 and words[0] in RELATIONS:
                d["rejected"].append([words[0], _norm(words[1])])
        elif line.startswith("- ") and section in RELATIONS:
            add(d, section, line[2:], source=CONFIRMED)
    rejected = [tuple(r) for r in d["rejected"]]
    d["facts"] = [f for f in d["facts"] if (f["rel"], _norm(f["what"])) not in rejected]
    return d


def summary(d: dict | None = None, limit: int = SUMMARY_CHARS, practice: bool = False) -> str:
    """What every agent is told about you: confirmed facts, then the most seen;
    a guess says so. Within `limit` characters; what you are practising only
    when `practice`."""
    d = prune(import_about_me(load())) if d is None else d
    out, size = [], 0
    for f in _ordered(d):
        if f["rel"] == PRACTICE and not practice:
            continue
        line = f"- {f['rel']}：{f['what']}" + ("" if confirmed(f) or _seen(f) >= _SURE else "（推斷）")
        if size + len(line) > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _yours(text: str) -> str:
    """The person's part of about_me.md: everything above meeseek's heading."""
    m = _INFERRED_AT.search(text)
    return text[:m.start()] if m else text


def render_inferred(d: dict) -> str:
    """meeseek's part of about_me.md: what it inferred and has not been
    confirmed, each with how often it was heard and the words it rests on."""
    out = [INFERRED, "",
           "從你的對話（主控台和手機）和會後報告推斷的。確定的就把那一行搬到上面對應的標題下；"
           "不成立的寫到上面的「不成立」（例如「- 不要 用表格」）。只聽過一次、一個月沒再聽到的會自己拿掉。", ""]
    for rel in RELATIONS:
        facts = [f for f in _ordered(d) if f["rel"] == rel and not confirmed(f)]
        if facts:
            out += [f"### {rel}", ""]
            for f in facts:
                out.append(f"- {f['what']}")
                # one line each: a quote's own line breaks would leave the bullet
                out += [f"  - {_label(f)}"] + [f"  - {' '.join(ev.split())}" for ev in f["evidence"]]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_about_me(d: dict) -> None:
    """Rewrite meeseek's part of about_me.md and keep the person's as it is on
    disk at this moment — read again under the lock right before writing, so an
    edit saved meanwhile is never lost (the console's editor also carries the
    version it opened, runtime/context_store.py)."""
    from meeseek.runtime.store import path_lock

    about_me_file().parent.mkdir(parents=True, exist_ok=True)
    with path_lock(about_me_file()):
        text = about_me_file().read_text(encoding="utf-8") if about_me_file().exists() else ABOUT_ME_TEMPLATE
        about_me_file().write_text(_yours(text).rstrip() + "\n\n" + render_inferred(d), encoding="utf-8")


def save(d: dict) -> None:
    d = prune(d)
    store_file().parent.mkdir(parents=True, exist_ok=True)
    store_file().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    write_about_me(d)


# --- what it learns from ------------------------------------------------------------------

def note_practice(report: str, source: str) -> int:
    """A coach report's 「下次練習」, as what you are practising."""
    m = _PRACTICE.search(report)
    if not m:
        return 0
    d = import_about_me(load())
    # What you practise is what the last report said: the one before is done with.
    d["facts"] = [f for f in d["facts"] if f["rel"] != PRACTICE or confirmed(f)]
    n = sum(add(d, PRACTICE, re.sub(r"^[-*\d.\s]+", "", ln).strip(), source=source)
            for ln in m.group(1).splitlines() if ln.strip())
    save(d)
    return n


# The console's sessions, the old dev UI's, and the Inbox's — where what you
# send from your phone lands; only what you typed there is read (`past_sessions.typed`).
CHAT_USERS = ("webui", "user", "inbox")


def _conversations(since: float) -> list[tuple[str, str]]:
    """(source, text) per session since `since` — the source names the day
    and the session: your messages, each after the opening of the reply before
    it, for what you were answering."""
    from meeseek.runtime import history
    from meeseek.runtime.past_sessions import typed

    by_session: dict[str, list[str]] = {}
    dates: dict[str, str] = {}
    last_reply: dict[str, str] = {}
    for r in sorted(history.rows(since=since, users=CHAT_USERS), key=lambda r: (r.session, r.at)):
        sid, user, ts, author, text = r.session, r.user, r.at, r.event.get("author"), history.text_of(r.event)
        if not text or not text.strip():
            continue
        if author == "user":
            text = typed(user, text)
            if not text:
                continue
            before = last_reply.pop(sid, "")
            by_session.setdefault(sid, []).append(
                (f"（助理：{before[:200]}…）\n" if before else "") + f"使用者：{text.strip()[:1500]}")
            dates.setdefault(sid, datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))
        else:
            last_reply[sid] = text.strip()
    return [(f"對話 {dates[sid]} {sid[:6]}", "\n\n".join(msgs)) for sid, msgs in by_session.items()]


class _Fact(BaseModel):
    conversation: int = Field(description="出自第幾段對話（［對話 N］的 N）")
    rel: str = Field(description="偏好 / 不要 / 習慣 / 擅長 / 目標 / 關注")
    what: str = Field(description="可以直接照著做的短句")
    quote: str = Field(description="使用者自己說的原句，60 字內")


class _Facts(BaseModel):
    facts: list[_Fact]


def learn_from_chat(days: float = 1, me: str = "") -> int:
    """Read your conversations of the last `days` for what holds of you; how many facts."""
    from meeseek.runtime.oneshot import ask

    convos = _conversations(time.time() - days * 86400)
    if not convos:
        return 0
    d = import_about_me(load())
    n = 0
    batch: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal n
        if not batch:
            return
        text = "\n\n=====\n\n".join(f"［對話 {i}］\n{body}" for i, (_, body) in enumerate(batch, 1))
        facts = ask(prompts.text("internal/personal_learn", me=me or "使用者", known=summary(d) or "（還沒有）",
                                 conversations=text[:80000]),
                    model=cfg.models.workflow, schema=_Facts, temperature=0.1, max_output_tokens=6000).facts
        for f in facts:
            if f.rel != "正在練習" and 1 <= f.conversation <= len(batch):
                n += add(d, f.rel, f.what, source=batch[f.conversation - 1][0], quote=f.quote)
        batch.clear()

    size = 0
    for source, body in sorted(convos):
        if size + len(body) > 60000:
            flush()
            size = 0
        batch.append((source, body))
        size += len(body)
    flush()
    save(d)
    return n
