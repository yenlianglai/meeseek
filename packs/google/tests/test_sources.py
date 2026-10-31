"""What the Google pack's sources settle from what they see, and how they report."""

from meeseek_pack_google import sources


def test_bulk_mail_by_its_headers():
    assert sources._is_bulk({"list-unsubscribe": "<x>"})
    assert sources._is_bulk({"from": "GitLab <noreply@gitlab.com>"})
    assert sources._is_bulk({"auto-submitted": "auto-generated"})
    assert not sources._is_bulk({"from": "Heidi <heidi@example.com>"})


def test_an_invitation_is_found_in_any_part():
    assert sources._is_invite({"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain"}, {"mimeType": "multipart/alternative",
                                     "parts": [{"mimeType": "text/calendar"}]}]})
    assert not sources._is_invite({"mimeType": "text/plain"})


def test_only_mentions_of_me():
    msg = {"annotations": [{"type": "USER_MENTION", "userMention": {"user": {"name": "users/me1"}}}]}
    assert sources._mentions(msg, "users/me1")
    assert not sources._mentions(msg, "users/other")
    assert not sources._mentions({}, "users/me1")


class _Events:
    def __init__(self, items):
        self.items = items

    def list(self, **kw):
        return type("R", (), {"execute": lambda _: {"items": self.items}})()


def test_a_meeting_about_to_start_is_a_meeting_item(monkeypatch):
    ev = {"id": "e1", "summary": "Sprint review", "start": {"dateTime": "2026-10-10T10:00:00+08:00"},
          "end": {"dateTime": "2026-10-10T11:00:00+08:00"}, "hangoutLink": "https://meet.google.com/abc",
          "attendees": [{"email": "me@x.com", "self": True}, {"displayName": "Heidi"}],
          "organizer": {"email": "heidi@x.com"}}
    declined = {**ev, "id": "e2", "attendees": [{"self": True, "responseStatus": "declined"}, {"displayName": "H"}]}
    alone = {**ev, "id": "e3", "attendees": [{"self": True}]}
    svc = type("S", (), {"events": lambda _: _Events([ev, declined, alone])})()
    monkeypatch.setattr(sources, "build_service", lambda *a, **k: svc)
    monkeypatch.setattr(sources, "get_credentials", lambda: object())
    out = sources.calendar_poll(lead_minutes=10, state={"k": 1})
    [item] = out["items"]
    assert item["kind"] == "meeting" and item["key"] == "calendar:e1:2026-10-10T10:00:00+08:00"
    assert item["meta"]["attendees"] == ["Heidi"] and item["meta"]["meet"] == "https://meet.google.com/abc"
    assert out["state"] == {"k": 1}


def test_a_calendar_event_is_a_meeting_in_terms_that_are_not_googles():
    from meeseek_pack_google.drive import _meeting

    ev = {"id": "e1", "summary": "Weekly", "hangoutLink": "https://meet.google.com/abc-defg-hij",
          "start": {"dateTime": "2026-10-10T10:00:00+08:00"}, "end": {"dateTime": "2026-10-10T11:00:00+08:00"},
          "attendees": [{"email": "me@x.com", "displayName": "Me", "self": True, "responseStatus": "declined"},
                        {"displayName": "Heidi"}],
          "attachments": [{"mimeType": "text/plain", "fileId": "N"}, {"mimeType": "video/mp4", "fileId": "V", "title": "Rec"}]}
    m = _meeting(ev)
    assert (m["code"], m["title"], m["me"], m["declined"]) == ("abc-defg-hij", "Weekly", "Me me@x.com", True)
    assert m["recording"] == {"id": "V", "name": "Rec"} and m["attendees"] == ["Heidi"]
    assert _meeting({"id": "e2", "summary": "Lunch"})["code"] == "" and _meeting({"id": "e2"})["recording"] is None


class _Exec:
    def __init__(self, out):
        self.out = out

    def execute(self):
        return self.out


def _chat(spaces, messages):
    """A Chat service answering `spaces` and each space's `messages`."""
    msgs = type("M", (), {"list": lambda _, parent, **kw: _Exec({"messages": messages[parent]})})()
    sp = type("Sp", (), {"list": lambda _, **kw: _Exec({"spaces": spaces}),
                         "messages": lambda _: msgs})()
    return type("S", (), {"spaces": lambda _: sp})()


def test_a_space_is_one_item_and_your_word_there_answers_it(monkeypatch):
    def m(name, who, text, at):
        return {"name": name, "sender": {"name": who, "displayName": who.split("/")[1]},
                "text": text, "createTime": at}

    spaces = [{"name": "spaces/D", "spaceType": "DIRECT_MESSAGE", "lastActiveTime": "2026-10-28T10:00:00Z"},
              {"name": "spaces/R", "spaceType": "DIRECT_MESSAGE", "lastActiveTime": "2026-10-28T10:00:00Z"}]
    messages = {
        "spaces/D": [m("spaces/D/messages/1", "users/heidi", "在嗎", "2026-10-28T09:00:00Z"),
                     m("spaces/D/messages/2", "users/heidi", "看一下 log", "2026-10-28T09:01:00Z")],
        "spaces/R": [m("spaces/R/messages/1", "users/sam", "問一下", "2026-10-28T09:00:00Z"),
                     m("spaces/R/messages/2", "users/me", "好", "2026-10-28T09:05:00Z")],
    }
    monkeypatch.setattr(sources, "_chat_svc", lambda: _chat(spaces, messages))
    out = sources.chat_poll(since=sources._ts("2026-10-28T08:00:00Z"), state={"me": "users/me"})
    [item] = out["items"]
    assert item["group"] == "spaces/D" and item["key"] == "chat:spaces/D/messages/2"
    assert item["body"] == "heidi: 在嗎\nheidi: 看一下 log"
    assert out["answered"] == [{"group": "spaces/R", "at": sources._ts("2026-10-28T09:05:00Z")}]
