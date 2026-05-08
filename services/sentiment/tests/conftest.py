"""Test configuration for bush_sentiment.

bush_sentiment/__init__.py imports torch, transformers, paho.mqtt at module
load time. We stub all three before import so the test process never pulls
the heavy ML deps. We also add the sibling bushutil workspace package to
sys.path so `from bushutil import get_mqtt_broker` resolves.
"""
from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BUSHUTIL_SRC = _REPO_ROOT / "packages" / "bushutil" / "src"
if _BUSHUTIL_SRC.exists():
    sys.path.insert(0, str(_BUSHUTIL_SRC))


@pytest.fixture(autouse=True)
def stub_heavy_deps(monkeypatch):
    """Stub torch, transformers, paho.mqtt at sys.modules so import is cheap."""
    fakes: dict[str, MagicMock] = {}
    for mod_name, members in [
        ("torch", ["set_num_threads"]),
        ("transformers", ["pipeline"]),
        ("paho", []),
        ("paho.mqtt", []),
        ("paho.mqtt.client", ["Client", "CallbackAPIVersion"]),
    ]:
        m = MagicMock()
        for member in members:
            setattr(m, member, MagicMock())
        fakes[mod_name] = m
        monkeypatch.setitem(sys.modules, mod_name, m)

    # Drop any cached bush_sentiment so each test re-imports against fresh stubs.
    for cached in list(sys.modules):
        if cached.startswith("bush_sentiment"):
            monkeypatch.delitem(sys.modules, cached, raising=False)

    yield fakes
