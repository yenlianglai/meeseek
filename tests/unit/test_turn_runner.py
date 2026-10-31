"""The turn runner and its policies: the invariants a turn holds, whatever the model does.

Translated from the Go reference (meeseek-personal internal/agent and
internal/harness tests), on a scripted model and an in-memory store.
"""

import asyncio

import pytest

from meeseek.adapters.memory import MemoryEventStore
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.core import events as ev
from meeseek.core import ledger
from meeseek.core.policies import (
    ApprovalGate,
    CallBudget,
    Clock,
    Consent,
    Evidence,
    FailedWriteCheck,
    PromiseCheck,
    ResumedWrites,
)
from meeseek.core.tools import Effect, tool
from meeseek.core.turn import (
    RUNNER_SAY,
    UNKNOWN_OUTCOME,
    ContextOverflowError,
    NothingToResume,
    TransientModelError,
    TurnRunner,
    is_unknown,
)


class Rig:
    def __init__(self, *steps, policies=None, calls=5, minutes=15):
        self.model = Scripted(*steps)
        self.store = MemoryEventStore()
        self.ran: list[tuple[str, dict]] = []
        self.wakes: list[dict] = []
        self.queued: list[tuple[str, dict]] = []
        self.clock = 1_000_000.0

        @tool()
        def lookup() -> str:
            """Count the tickets."""
            self.ran.append(("lookup", {}))
            return "42 tickets"

        @tool(Effect.WRITE, target=["channel"])
        def post(channel: str, text: str = "") -> str:
            """Post to a channel."""
            self.ran.append(("post", {"channel": channel}))
            if channel == "broken":
                raise RuntimeError("channel is archived")
            return "posted"

        @tool(Effect.WRITE, target=["minutes"])
        def schedule_followup(minutes: int, why: str) -> str:
            """Wake this session later."""
            self.wakes.append({"minutes": minutes, "why": why})
            return f"scheduled in {minutes} min"

        @tool(shows_raw=True)
        def query_logs(env: str) -> str:
            """Read logs."""
            return "2 lines:\n```\nERROR boom\nINFO ok\n```"

        @tool(Effect.WRITE, target=["rule"])
        def remember_fact(rule: str) -> str:
            """Remember something for every later turn."""
            self.ran.append(("remember_fact", {"rule": rule}))
            return "remembered"

        @tool()
        def ask_user(question: str) -> dict:
            """Ask the person; their answer is the next turn."""
            return {"status": "waiting for the user's answer"}

        self.specs = [f.spec for f in (lookup, post, schedule_followup, query_logs, remember_fact, ask_user)]
        by_name = {s.name: s for s in self.specs}
        rig = self

        class Queue:
            def hold(self, t, c, spec):
                rig.queued.append((c.name, c.args))
                return f"a{len(rig.queued)}"

        self.approver_says = (True, "")

        class Approver:
            asked: list = []

            async def approve(self, t, c, spec):
                Approver.asked.append(c.name)
                return rig.approver_says

        self.approver = Approver()
        writes = lambda n: n in by_name and by_name[n].writes  # noqa: E731
        default = [ResumedWrites(writes), CallBudget(calls), Clock(minutes, now=lambda: self.clock), ApprovalGate(Queue(), self.approver),
                   Consent({"remember_fact": "this fact"}),
                   FailedWriteCheck(writes, lambda n: by_name[n].target if n in by_name else ()),
                   PromiseCheck(backed=lambda session: bool(self.wakes)),
                   Evidence(lambda n: n in by_name and by_name[n].shows_raw)]
        self.runner = TurnRunner(model=self.model, tools=self.specs, store=self.store,
                                 policies=default if policies is None else policies,
                                 system=lambda t: "You are meeseek." + ("" if t.watched else " Nobody is watching."),
                                 now=lambda: self.clock, sleep=self._sleep)

    async def _sleep(self, s):
        pass

    def run(self, text, watched=True):
        return asyncio.run(self.runner.run("s1", text, watched=watched))


def test_a_read_feeds_its_result_back_paired_with_its_call():
    r = Rig(call("c1", "lookup"), say("There are 42."))
    assert r.run("how many?").reply == "There are 42."
    last = r.model.seen[1].messages[-1]
    assert (last.role, last.text, last.call_id) == ("tool", "42 tickets", "c1")


def test_the_session_carries_across_turns_from_the_store():
    r = Rig(say("Noted."), say("You said blue."))
    r.run("my colour is blue")
    r.run("what colour?")
    assert [(m.role, m.text) for m in r.model.seen[1].messages] == [
        ("user", "my colour is blue"), ("assistant", "Noted."), ("user", "what colour?")]


def test_a_watched_write_waits_for_yes():
    r = Rig(call("c1", "post", channel="c"), say("Posted."))
    r.run("post it")
    assert r.approver.asked == ["post"] and r.ran == [("post", {"channel": "c"})]

    r = Rig(call("c1", "post", channel="c"), say("OK, I won't."))
    r.approver_says = (False, "not today")
    r.run("post it")
    assert r.ran == []
    told = r.model.seen[1].messages[-1].text
    assert "declined" in told and "not today" in told


def test_an_unwatched_turn_holds_writes_without_asking():
    r = Rig(call("c1", "post", channel="c"), say("I'd post the summary to c."))
    r.approver.asked.clear()
    res = r.run("[wake] post the summary", watched=False)
    assert r.approver.asked == [] and r.ran == [] and r.queued == [("post", {"channel": "c"})]
    assert [c.name for c in res.held] == ["post"]
    assert "a1" in r.model.seen[1].messages[-1].text


def test_past_the_call_budget_tools_say_answer_now():
    r = Rig(call("c1", "lookup"), call("c2", "lookup"), say("Partial answer."), calls=1)
    r.run("dig")
    assert len(r.ran) == 1 and "budget" in r.model.seen[2].messages[-1].text


def test_a_tool_past_its_advised_count_is_told_to_converge_then_blocked():
    steps = [call(f"c{i}", "lookup") for i in range(5)] + [say("done")]
    r = Rig(*steps, policies=[CallBudget(0, per_tool={"lookup": 2})])
    r.run("dig")
    tool_msgs = [m for m in r.model.seen[-1].messages if m.role == "tool"]
    assert "Converge" in tool_msgs[2].text                    # the third: ran, with a note
    assert "looping" in tool_msgs[4].text and len(r.ran) == 4  # the fifth: blocked


def test_an_unwatched_turn_has_a_clock():
    r = Rig(call("c1", "lookup"), call("c2", "lookup"), say("Out of time."))
    r.model.on_step = lambda n: setattr(r, "clock", r.clock + (3600 if n == 2 else 0))
    r.run("[wake] dig", watched=False)
    assert len(r.ran) == 1


def test_a_watched_turn_has_no_clock():
    r = Rig(call("c1", "lookup"), call("c2", "lookup"), say("Done."))
    r.model.on_step = lambda n: setattr(r, "clock", r.clock + 3600)
    r.run("dig")
    assert len(r.ran) == 2


def test_a_hidden_failure_is_sent_back_once_then_annotated():
    r = Rig(call("c1", "post", channel="broken"), say("Posted!"), say("Posted!!"))
    res = r.run("post it")
    told = r.model.seen[2].messages[-1]
    assert told.role == "user" and "channel is archived" in told.text
    assert res.reply.startswith("Posted!!") and "did not succeed" in res.reply


def test_an_honest_reply_passes_untouched():
    r = Rig(call("c1", "post", channel="broken"), say("Posting failed: the channel is archived."))
    res = r.run("post it")
    assert res.reply == "Posting failed: the channel is archived." and len(r.model.seen) == 2


def test_an_earlier_turns_failure_counts_when_the_reply_talks_about_it():
    r = Rig(call("c1", "post", channel="broken"), say("That failed: archived."),
            say("Posted to broken again, all good."), say("Sorry — posting to broken failed earlier."))
    r.run("post it")
    res = r.run("is it on broken now?")
    assert "channel is archived" in r.model.seen[3].messages[-1].text and "failed" in res.reply


def test_a_promise_gets_kept_by_scheduling_it():
    r = Rig(say("I'll get back to you when the build is done."),
            call("c1", "schedule_followup", minutes=30, why="check the build and report"),
            say("I'll get back to you when the build is done."))
    res = r.run("tell me when the build is done")
    assert "System record" not in res.reply and r.wakes == [{"minutes": 30, "why": "check the build and report"}]
    assert "nothing is scheduled" in r.model.seen[1].messages[-1].text


def test_an_unkept_promise_ships_with_a_note():
    r = Rig(say("我晚點再回報"), say("我晚點再回報"))
    res = r.run("看一下")
    assert res.reply.startswith("我晚點再回報") and "not scheduled" in res.reply


def test_a_cut_short_turn_is_repaired_on_the_next_one():
    r = Rig(say("Let me check whether it went out."))
    dangling = ev.ToolCall(id="c9", name="post", args={"channel": "c"})
    r.store.append([ev.UserSaid(session="s1", turn="old", text="post it"),
                    ev.ModelStepped(session="s1", turn="old", calls=(dangling,))])
    r.run("did it post?")
    m = r.model.seen[0].messages[2]
    assert (m.role, m.call_id, m.text) == ("tool", "c9", UNKNOWN_OUTCOME)


def test_transient_model_errors_are_retried():
    class Flaky:
        calls = 0

        async def step(self, **kw):
            Flaky.calls += 1
            if Flaky.calls <= 2:
                raise TransientModelError("503")
            return ev.ModelStepped(session=kw["session"], turn=kw["turn"], text="ok")

    r = Rig()
    r.runner.model = Flaky()
    assert r.run("hi").reply == "ok" and Flaky.calls == 3

    Flaky.calls = -10
    with pytest.raises(TransientModelError):
        r.run("hi")


def test_the_context_reaches_the_model():
    r = Rig(say("ok"))
    r.run("hi", watched=False)
    assert "Nobody is watching" in r.model.seen[0].system


def test_a_rule_waits_for_the_persons_yes():
    r = Rig(call("c1", "remember_fact", rule="prod is acme-prod"), say("Shall I remember that?"),
            call("c2", "remember_fact", rule="prod is acme-prod"), say("Remembered."))
    res = r.run("prod is acme-prod")
    assert r.ran == [] and res.held == []                      # not queued: simply not done
    r.run("好，記住這個")
    assert r.ran == [("remember_fact", {"rule": "prod is acme-prod"})]


def test_raw_evidence_reaches_the_reply():
    r = Rig(call("c1", "query_logs", env="uat"), say("uat has one ERROR."))
    res = r.run("uat logs?")
    assert res.reply.startswith("```\nERROR boom") and res.reply.endswith("uat has one ERROR.")


def test_an_unknown_tool_or_a_failing_one_is_the_models_to_read():
    r = Rig(call("c1", "nope"), call("c2", "post", channel="broken"), say("post failed; nope does not exist"))
    r.run("go")
    tool_msgs = [m for m in r.model.seen[-1].messages if m.role == "tool"]
    assert "no tool named 'nope'" in tool_msgs[0].text and "channel is archived" in tool_msgs[1].text


def test_every_step_is_on_the_record():
    r = Rig(call("c1", "post", channel="broken"), say("Posted!"), say("Posting failed."))
    r.run("post it")
    kinds = [e.kind for e in r.store.read("s1")]
    assert kinds == ["user_said", "model_stepped", "tool_ran", "model_stepped", "policy_spoke",
                     "model_stepped", "replied"]


# --- the ledger, from the Go harness tests ------------------------------------------

def _stream(*calls):
    """(tool, args, error-or-None) → a turn's events."""
    out = []
    for i, (name, args, err) in enumerate(calls):
        c = ev.ToolCall(id=f"c{i}", name=name, args=args)
        out += [ev.ModelStepped(session="s", turn="t", calls=(c,)),
                ev.ToolRan(session="s", turn="t", call=c, result=f"Error: {err}" if err else "ok", failed=bool(err))]
    return out


WRITES = {"create_issue", "link_epic", "post"}
LINK_FAILED = _stream(("create_issue", {"title": "x"}, None), ("search", {"q": "y"}, "boom"),
                      ("link_epic", {"issue_key": "AB-17"}, "not an epic"))


def _unreported(stream, reply):
    return ledger.unreported(ledger.ledger(stream, "t", WRITES.__contains__), reply)


def test_a_reply_that_skips_the_failure_is_caught():
    assert [e.tool for e in _unreported(LINK_FAILED, "Created the issue and linked it under the epic.")] == ["link_epic"]


def test_saying_it_failed_passes_in_either_language():
    assert _unreported(LINK_FAILED, "Created the issue; linking to the epic failed.") == []
    assert _unreported(LINK_FAILED, "建好了，但掛 epic 沒成功") == []


def test_naming_the_target_is_not_saying_it_failed():
    assert [e.tool for e in _unreported(LINK_FAILED, "Linked AB-17 under the epic.")] == ["link_epic"]


def test_a_later_success_on_the_same_target_settles_it():
    retried = LINK_FAILED + _stream(("link_epic", {"issue_key": "AB-17"}, None))
    assert _unreported(retried, "All linked.") == []


def test_an_earlier_success_does_not_settle_a_later_failure():
    stream = _stream(("post", {"channel": "c1"}, None), ("post", {"channel": "c1"}, "rate limited"))
    assert len(_unreported(stream, "Posted.")) == 1


@pytest.mark.parametrize("text", ["交給它了，完成後會回報給你。", "I'll get back to you when it's done.", "我晚點再回報"])
def test_promises_are_found(text):
    assert ledger.promise(text)


@pytest.mark.parametrize("text", ["Done — 12 tickets are in the sprint.", "結果可以之後再問我", "You'll get a reply from them."])
def test_no_false_promises(text):
    assert ledger.promise(text) == ""


@pytest.mark.parametrize("answer,written", [("好", True), ("不要，先不用", False)])
def test_an_answered_question_is_consent_unless_it_refuses(answer, written):
    r = Rig(call("q", "ask_user", question="要把 prod 叫 acme-prod 記下來嗎？"), say("等你回答。"),
            call("c1", "remember_fact", rule="prod is acme-prod"), say("ok"))
    r.run("prod 叫 acme-prod")
    r.run(answer)
    assert (r.ran == [("remember_fact", {"rule": "prod is acme-prod"})]) is written


# --- several agents ---------------------------------------------------------------

def _agents():
    from meeseek.core.turn import Agent

    @tool()
    def sprint_board() -> str:
        """The sprint."""
        return "12 tickets"

    router = Scripted(call("t1", "transfer_to_agent", agent_name="sprint"), agent="router")
    sprint = Scripted(call("s1", "sprint_board"), say("12 tickets in the sprint."), say("Still 12."), agent="sprint")
    agents = [Agent(name="router", model=router, system=lambda t: "route", description="routes"),
              Agent(name="sprint", model=sprint, system=lambda t: "sprint SOP", tools=[sprint_board.spec],
                    description="the sprint board")]
    return router, sprint, agents


def test_the_router_hands_over_and_the_skill_answers():
    router, sprint, agents = _agents()
    store = MemoryEventStore()
    runner = TurnRunner(store=store, agents=agents, root="router")
    res = asyncio.run(runner.run("s", "how is the sprint?"))
    assert res.reply == "12 tickets in the sprint."
    assert "transfer_to_agent" in router.seen[0].tools and "sprint_board" not in router.seen[0].tools
    assert sprint.seen[0].system == "sprint SOP" and "sprint_board" in sprint.seen[0].tools
    assert [e.by for e in store.read("s") if e.kind == "replied"] == ["sprint"]


def test_the_next_turn_starts_with_the_agent_that_answered():
    router, sprint, agents = _agents()
    runner = TurnRunner(store=MemoryEventStore(), agents=agents, root="router")
    asyncio.run(runner.run("s", "how is the sprint?"))
    assert asyncio.run(runner.run("s", "and now?")).reply == "Still 12." and len(router.seen) == 1


def test_handing_over_to_nobody_is_the_models_to_read():
    from meeseek.core.turn import Agent

    m = Scripted(call("t", "transfer_to_agent", agent_name="ghost"), say("ok"), agent="a")
    runner = TurnRunner(store=MemoryEventStore(), root="a",
                        agents=[Agent(name="a", model=m), Agent(name="b", model=Scripted(), description="b")])
    asyncio.run(runner.run("s", "x"))
    assert "no agent 'ghost'" in m.seen[1].messages[-1].text


def test_a_policy_clamps_arguments_and_shapes_results():
    from meeseek.core.turn import BasePolicy, Run

    seen_args = []

    @tool()
    def logs(lines: int = 1000) -> str:
        """Tail logs."""
        seen_args.append(lines)
        return "x" * 500

    class Clamp(BasePolicy):
        name = "clamp"

        async def on_tool(self, t, c, spec):
            return Run(args={**c.args, "lines": min(int(c.args.get("lines", 1000)), 200)})

        def on_result(self, t, c, spec, result):
            return result[:10] + "…(cut)"

    m = Scripted(call("c", "logs", lines=5000), say("done"))
    store = MemoryEventStore()
    asyncio.run(TurnRunner(model=m, tools=[logs.spec], store=store, policies=[Clamp()]).run("s", "logs"))
    assert seen_args == [200]
    ran = [e for e in store.read("s") if e.kind == "tool_ran"][0]
    assert ran.call.args == {"lines": 200} and ran.result == "xxxxxxxxxx…(cut)"


def test_a_retry_waits_no_longer_than_the_ceiling_and_is_told():
    from meeseek.core.turn import MAX_BACKOFF_S

    class Flaky:
        calls = 0

        async def step(self, **kw):
            Flaky.calls += 1
            if Flaky.calls <= 6:
                raise TransientModelError("503 overloaded")
            return ev.ModelStepped(session=kw["session"], turn=kw["turn"], text="ok")

    slept, told = [], []
    r = Rig()
    r.runner.model, r.runner.retries = Flaky(), 10

    async def sleep(s):
        slept.append(s)

    r.runner.sleep = sleep
    asyncio.run(r.runner.run("s1", "hi", on_retry=lambda *a: told.append(a)))
    assert slept == [4.0, 8.0, 16.0, MAX_BACKOFF_S, MAX_BACKOFF_S, MAX_BACKOFF_S]
    assert [a[0] for a in told] == [1, 2, 3, 4, 5, 6] and told[0][3] == "503 overloaded"



# --- a turn cut short is carried on, not asked again --------------------------------

def _cut_short(r, *events):
    """The store holds turn "t1": the person's message, then `events`, and no reply."""
    r.store.append([ev.UserSaid(session="s1", turn="t1", text="how many, and post it"), *events])


def _resume(r):
    return asyncio.run(r.runner.resume("s1"))


def test_a_turn_cut_short_after_a_read_carries_on_from_its_result():
    r = Rig(say("There are 42."))
    c = ev.ToolCall(id="c1", name="lookup")
    _cut_short(r, ev.ModelStepped(session="s1", turn="t1", calls=(c,)),
               ev.ToolRan(session="s1", turn="t1", call=c, result="42 tickets"))
    done = _resume(r)
    assert (done.reply, done.turn) == ("There are 42.", "t1") and r.ran == []
    assert [m.text for m in r.model.seen[0].messages if m.role == "user"] == ["how many, and post it"]
    assert sum(isinstance(e, ev.UserSaid) for e in r.store.read("s1")) == 1, "no second message from the person"
    assert any(isinstance(e, ev.Replied) and e.turn == "t1" for e in r.store.read("s1"))


def test_a_read_that_never_returned_is_run_again():
    r = Rig(say("There are 42."))
    _cut_short(r, ev.ModelStepped(session="s1", turn="t1", calls=(ev.ToolCall(id="c1", name="lookup"),)))
    _resume(r)
    assert r.ran == [("lookup", {})]


def test_a_write_that_never_returned_is_not_sent_again_until_a_read_has_checked():
    r = Rig(call("c2", "post", channel="ops"), call("c3", "lookup"), call("c4", "post", channel="ops"),
            say("Posted once; I checked first."))
    _cut_short(r, ev.ModelStepped(session="s1", turn="t1", calls=(ev.ToolCall(id="c1", name="post",
                                                                               args={"channel": "ops"}),)))
    done = _resume(r)
    ran = {e.call.id: e for e in done.events if isinstance(e, ev.ToolRan)}
    assert is_unknown(ran["c1"].result) and not ran["c1"].failed, "the in-flight write: outcome unknown"
    assert is_unknown(ran["c2"].result), "sent again before any read: held back again"
    assert r.ran == [("lookup", {}), ("post", {"channel": "ops"})], "after the read, it may go"
    assert RUNNER_SAY["resumed_write"][:20] in r.model.seen[0].messages[-1].text


def test_a_finished_turn_has_nothing_to_resume():
    r = Rig(say("done"))
    r.run("hi")
    with pytest.raises(NothingToResume):
        _resume(r)


def test_the_agent_that_was_answering_carries_on():
    r = Rig()
    router, sprint, agents = _agents()
    r.runner = TurnRunner(store=r.store, agents=agents, root="router")
    sprint.steps[:] = [say("sprint answer")]
    hand = ev.ToolCall(id="h", name="transfer_to_agent", args={"agent_name": "sprint"})
    _cut_short(r, ev.ModelStepped(session="s1", turn="t1", by="router", calls=(hand,)),
               ev.ToolRan(session="s1", turn="t1", by="sprint", call=hand, result="Transferred to sprint."))
    assert _resume(r).reply == "sprint answer" and router.seen == []


# --- a request over the context window is trimmed, not failed ----------------------

class Overflowing:
    """Refuses a request until the long results it is shown are at most `fits`."""

    def __init__(self, fits):
        self.fits, self.seen = fits, []

    async def step(self, *, history, session, turn, **kw):
        long = sum(1 for e in history if isinstance(e, ev.ToolRan) and len(str(e.result)) > 400)
        self.seen.append(long)
        if long > self.fits:
            raise ContextOverflowError("input token count exceeds the maximum")
        return ev.ModelStepped(session=session, turn=turn, text="answered")


def _big_history(r, n):
    for i in range(n):
        c = ev.ToolCall(id=f"b{i}", name="lookup")
        r.store.append([ev.UserSaid(session="s1", turn=f"o{i}", text="q"),
                        ev.ModelStepped(session="s1", turn=f"o{i}", calls=(c,)),
                        ev.ToolRan(session="s1", turn=f"o{i}", call=c, result="x" * 5000),
                        ev.Replied(session="s1", turn=f"o{i}", text="a")])


def test_an_overflowing_request_is_asked_again_with_older_results_trimmed():
    r = Rig()
    _big_history(r, 10)
    r.runner.model = Overflowing(fits=6)
    done = r.run("and now?")
    assert done.reply == "answered" and r.runner.model.seen == [10, 6]
    assert any(isinstance(e, ev.PolicySpoke) and e.policy == "context" for e in done.events)
    assert all(len(str(e.result)) == 5000 for e in r.store.read("s1") if isinstance(e, ev.ToolRan)), "kept whole"


def test_it_trims_harder_and_then_gives_up():
    r = Rig()
    _big_history(r, 10)
    r.runner.model = Overflowing(fits=1)
    assert r.run("and now?").reply == "answered" and r.runner.model.seen == [10, 6, 1]
    r.runner.model = Overflowing(fits=0)
    with pytest.raises(ContextOverflowError):
        r.run("again?")


# --- a tool call cut off by the output limit is not run --------------------------------

def test_a_cut_off_call_is_not_run_and_the_model_is_told_to_split_it():
    from meeseek.adapters.scripted import cut_off

    r = Rig(cut_off("w", "post", channel="ops"), say("Splitting it."))
    done = r.run("post the long report")
    [held] = [e for e in done.events if isinstance(e, (ev.ToolRan, ev.ToolHeld))]
    assert r.ran == [] and isinstance(held, ev.ToolHeld) and held.reason == RUNNER_SAY["split_the_call"]
    assert done.reply == "Splitting it." and r.model.seen[1].messages[-1].text == RUNNER_SAY["split_the_call"]


def test_unparseable_arguments_are_a_cut_off_call():
    r = Rig(call("w", "post", _unparsed='{"channel": "op'), say("Splitting it."))
    done = r.run("post the long report")
    assert r.ran == [] and [e.reason for e in done.events if isinstance(e, ev.ToolHeld)] == [RUNNER_SAY["split_the_call"]]
