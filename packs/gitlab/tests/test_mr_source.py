"""The MR source: what on your MRs needs you, one conversation per MR."""

from meeseek_pack_gitlab import sources

ME = {"id": 7, "username": "me"}
SINCE = sources._ts("2026-10-28T08:00:00Z")


def _mr(iid, **kw):
    return {"iid": iid, "project_id": 1, "references": {"full": f"grp/app!{iid}"}, "title": f"MR {iid}",
            "web_url": f"https://g/grp/app/-/merge_requests/{iid}", "state": "opened",
            "created_at": "2026-10-20T00:00:00Z", "author": {"id": 9, "name": "Heidi"}, "description": "d", **kw}


def _note(nid, who, body, at, system=False):
    return {"id": nid, "author": {"id": who, "name": "Me" if who == 7 else "Heidi"}, "body": body,
            "created_at": at, "system": system}


def _gitlab(monkeypatch, mrs_by_role, notes):
    def get(endpoint):
        if endpoint.startswith("merge_requests?"):
            role = next(r for r in ("reviewer", "assignee", "author") if f"{r}_id=" in endpoint)
            return mrs_by_role.get(role, [])
        iid = int(endpoint.split("/merge_requests/")[1].split("/")[0])
        return notes.get(iid, [])
    monkeypatch.setattr(sources, "_get", get)


def test_comments_for_you_file_your_word_answers_and_a_review_asked_is_news(monkeypatch):
    _gitlab(monkeypatch, {
        "author": [_mr(1, author={"id": 7, "name": "Me"})],
        "reviewer": [_mr(2), _mr(3), _mr(4, state="merged", merged_at="2026-10-28T09:30:00Z")],
    }, {
        1: [_note(11, 9, "這裡為什麼改？", "2026-10-28T09:00:00Z")],
        2: [_note(21, 9, "requested review from @me", "2026-10-28T09:00:00Z", system=True)],
        3: [_note(31, 9, "看一下", "2026-10-28T09:00:00Z"), _note(32, 7, "LGTM", "2026-10-28T09:10:00Z")],
        4: [],
    })
    out = sources.mr_poll(since=SINCE, state={"me": ME})
    by_group = {i["group"]: i for i in out["items"]}
    assert set(by_group) == {"mr:grp/app!1", "mr:grp/app!2"}
    assert by_group["mr:grp/app!1"]["body"] == "Heidi: 這裡為什麼改？" and by_group["mr:grp/app!1"]["kind"] == "mr"
    assert by_group["mr:grp/app!2"]["body"].startswith("Heidi 開的 MR（你是 reviewer）")   # a review asked: what the MR is
    assert {a["group"] for a in out["answered"]} == {"mr:grp/app!3", "mr:grp/app!4"}      # you replied; it merged


def test_your_own_user_is_asked_once(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_get", lambda e: calls.append(e) or ({"id": 7, "username": "me"} if e == "user" else []))
    state = sources.mr_poll(since=SINCE)["state"]
    sources.mr_poll(since=SINCE, state=state)
    assert calls.count("user") == 1
