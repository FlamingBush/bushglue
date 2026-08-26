"""Regression tests for the bush-ptt button watch loop.

The original loop debounced by discarding edges: an edge arriving inside the
bounce window was dropped without reconciling state. Observed in the field —
press, 58 ms release, bounce press, then the real release landed inside the
window and was thrown away. The line then sat still with no further edges, so
the service believed the button was held indefinitely, and only the
endpointer's 30 s stuck-button guard recovered it.

The loop now treats the level as the truth and re-reads it every iteration,
including the poll-timeout path, so a missed edge self-heals within one tick.
"""
import importlib
import json
import sys
import threading

import pytest


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "bush_ptt" in sys.modules:
        del sys.modules["bush_ptt"]
    return importlib.import_module("bush_ptt")


class FakeLine:
    """A GPIO line whose level is scripted; poll() reports pending edges.

    Stops the loop from inside read(), which every iteration reaches — the
    edge branch is not guaranteed to run, so counting there would hang.
    """

    def __init__(self, levels, edges):
        # Each list yields its values in order; the last value repeats.
        self._levels = list(levels)
        self._edges = list(edges)
        self.closed = False
        self._stop = None
        self._limit = 0
        self._reads = 0

    def bind_stop(self, stop, limit):
        self._stop, self._limit = stop, limit

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def poll(self, timeout=0):
        return self._next(self._edges)

    def read_event(self):
        return None

    def read(self):
        self._reads += 1
        if self._stop is not None and self._reads >= self._limit:
            self._stop.set()
        return self._next(self._levels)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))


def _run_watch(mod, line, iterations=6):
    """Run watch() until FakeLine trips the stop event, then return the client.

    Runs in a thread with a join timeout so a regression that fails to notice
    the stop event fails the test instead of hanging the suite.
    """
    client = FakeClient()
    stop = threading.Event()
    line.bind_stop(stop, iterations)
    mod._open_line = lambda: line

    t = threading.Thread(target=mod.watch, args=(client, stop), daemon=True)
    t.start()
    t.join(timeout=5)
    stop.set()
    assert not t.is_alive(), "watch() did not exit — loop ignored the stop event"
    return client


def _states(client):
    return [json.loads(p)["pressed"]
            for t, p in client.published if t.endswith("stt/ptt")]


@pytest.fixture
def mod(monkeypatch):
    return _reload(monkeypatch, PTT_LED_LINE="", PTT_DEBOUNCE_MS="1")


def test_release_is_seen_even_when_its_edge_is_missed(mod):
    """The field bug: the level goes high but no edge is ever reported again."""
    # Held (low), then released (high) with NO further edge to announce it.
    line = FakeLine(levels=[False, False, True], edges=[True, False])
    client = _run_watch(mod, line)
    states = _states(client)
    assert states[-1] is False, (
        f"release never observed — latched pressed. states={states}")


def test_steady_state_does_not_republish(mod):
    """No edges and no level change: the loop must stay quiet."""
    line = FakeLine(levels=[True], edges=[False])
    client = _run_watch(mod, line)
    assert _states(client) == [False]      # only the startup publish


def test_bounce_burst_settles_to_one_press(mod):
    """A bouncing press produces a single pressed=True, not a burst."""
    line = FakeLine(levels=[True, False], edges=[True, False])
    client = _run_watch(mod, line)
    states = _states(client)
    assert states.count(True) == 1, f"bounce leaked extra presses: {states}"


def test_line_is_released_on_exit(mod):
    line = FakeLine(levels=[True], edges=[False])
    _run_watch(mod, line)
    assert line.closed


def test_default_debounce_covers_the_observed_bounce(monkeypatch):
    """The field switch bounced past 25 ms, so the default must exceed that."""
    monkeypatch.delenv("PTT_DEBOUNCE_MS", raising=False)
    m = _reload(monkeypatch, PTT_LED_LINE="")
    assert m.PTT_DEBOUNCE_MS >= 50
    # ...and stay well under the endpointer's 250 ms minimum utterance.
    assert m.PTT_DEBOUNCE_MS < 250
