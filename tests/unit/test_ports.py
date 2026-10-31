"""Every adapter of a port passes the same contract."""

import pytest

from meeseek import ports
from meeseek.adapters.memory import MemoryEventStore, MemoryLease
from meeseek.adapters.sqlite import SqliteEventStore, SqliteLease
from meeseek.core import events as ev


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return MemoryEventStore() if request.param == "memory" else SqliteEventStore(tmp_path / "meeseek.db")


@pytest.fixture(params=["memory", "sqlite"])
def lease_pair(request, tmp_path):
    """Two handles on one lease, as two processes would have."""
    if request.param == "memory":
        one = MemoryLease()
        return one, one
    return SqliteLease(tmp_path / "meeseek.db"), SqliteLease(tmp_path / "meeseek.db")


def _said(session, text, at):
    return ev.UserSaid(session=session, turn="t", text=text, at=at)


class TestEventStore:
    def test_is_the_port(self, store):
        assert isinstance(store, ports.EventStore)

    def test_events_come_back_in_order_per_session(self, store):
        store.append([_said("a", "1", 1.0), _said("b", "x", 2.0)])
        store.append([_said("a", "2", 3.0)])
        assert [e.text for e in store.read("a")] == ["1", "2"]
        assert [e.text for e in store.read("a", after=1)] == ["2"]
        assert store.read("nobody") == []
        assert store.sessions() == ["a", "b"]

    def test_a_deleted_session_is_gone_and_the_rest_stay(self, store):
        store.append([_said("a", "1", 1.0), _said("b", "x", 2.0)])
        store.delete("a")
        assert store.read("a") == [] and store.sessions() == ["b"]
        store.append([_said("a", "again", 3.0)])
        assert [e.text for e in store.read("a")] == ["again"]

    def test_pruning_forgets_only_conversations_idle_since_the_cutoff(self, store):
        store.append([_said("old", "a", 100.0), _said("old", "b", 200.0)])
        store.append([_said("alive", "a", 100.0), _said("alive", "b", 900.0)])   # old start, recent turn
        assert store.prune(500.0) == 1
        assert store.sessions() == ["alive"] and store.read("old") == []

    def test_every_kind_survives_the_store(self, store):
        call = ev.ToolCall(id="c", name="kb_grep", args={"pattern": "x"})
        stream = [_said("s", "hi", 1.0),
                  ev.ModelStepped(session="s", turn="t", calls=(call,), usage=ev.Usage(model="m", input_tokens=3)),
                  ev.ToolRan(session="s", turn="t", call=call, result={"result": [1, "二"]}),
                  ev.ToolHeld(session="s", turn="t", call=call, reason="r"),
                  ev.PolicySpoke(session="s", turn="t", policy="harness", said="reconcile"),
                  ev.Replied(session="s", turn="t", text="好")]
        store.append(stream)
        assert store.read("s") == stream


class TestLease:
    def test_is_the_port(self, lease_pair):
        assert isinstance(lease_pair[0], ports.Lease)

    def test_one_holder_at_a_time(self, lease_pair):
        a, b = lease_pair
        assert a.acquire("scheduler", "proc-1", ttl_s=60)
        assert not b.acquire("scheduler", "proc-2", ttl_s=60)
        assert a.acquire("scheduler", "proc-1", ttl_s=60)          # renewing is taking it again
        assert b.holder("scheduler") == "proc-1"
        a.release("scheduler", "proc-2")                            # not theirs to give up
        assert b.holder("scheduler") == "proc-1"
        a.release("scheduler", "proc-1")
        assert b.acquire("scheduler", "proc-2", ttl_s=60)

    def test_an_expired_lease_is_free(self, lease_pair):
        a, b = lease_pair
        assert a.acquire("scheduler", "proc-1", ttl_s=-1)
        assert b.holder("scheduler") == ""
        assert b.acquire("scheduler", "proc-2", ttl_s=60)


def test_the_registry_is_a_tool_provider():
    from meeseek.tools.registry import RegistryTools

    assert isinstance(RegistryTools(), ports.ToolProvider)
