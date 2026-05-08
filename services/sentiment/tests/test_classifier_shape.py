"""Tests for the transformers v5.x classifier-output contract.

Background: transformers 5.x removed `return_all_scores=True`. The current
sentiment service uses `top_k=None` instead, which returns

    [[{"label": ..., "score": ...}, ...]]   # outer = batch dim

Subscribers (bush-flame-expression, bush-discord, bush-monitor) expect a
flat list of {label, score} dicts in the `classification` payload field, so
the service unwraps `[0]` before publishing.

These tests pin that contract so a future transformers bump that shifts the
shape again gets caught at CI time, not at the playa.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


SAMPLE_CLASSIFIER_OUTPUT = [
    [
        {"label": "anger", "score": 0.45},
        {"label": "joy", "score": 0.35},
        {"label": "fear", "score": 0.10},
        {"label": "sadness", "score": 0.05},
        {"label": "surprise", "score": 0.03},
        {"label": "love", "score": 0.02},
    ]
]


def _install_fake_classifier(output):
    """Install a fake classifier global into the freshly-imported module.

    Returns the imported module so callers can poke at internals.
    """
    import bush_sentiment

    bush_sentiment.classifier = MagicMock(return_value=output)
    return bush_sentiment


def test_classify_and_fire_unwraps_batch_dim_and_picks_top():
    mod = _install_fake_classifier(SAMPLE_CLASSIFIER_OUTPUT)
    fake_mqtt = MagicMock()

    scores, flare, bigjet = mod._classify_and_fire("a verse", fake_mqtt)

    assert scores == SAMPLE_CLASSIFIER_OUTPUT[0], "scores must be the unwrapped flat list (batch dim stripped)"
    assert isinstance(scores, list) and all("label" in s and "score" in s for s in scores)
    # anger has the top score → uses anger pattern.
    # flare = int(220 * 0.45) = 99; bigjet = int(700 * 0.45) = 315.
    assert flare == 99
    assert bigjet == 315


def test_classify_and_fire_unknown_label_does_not_crash():
    mod = _install_fake_classifier([[{"label": "ennui", "score": 0.99}]])
    fake_mqtt = MagicMock()

    scores, flare, bigjet = mod._classify_and_fire("a verse", fake_mqtt)

    # Unknown emotion → no pattern → zero pulse values, no exception.
    assert scores == [{"label": "ennui", "score": 0.99}]
    assert flare == 0
    assert bigjet == 0


def test_load_model_accepts_well_formed_output(monkeypatch):
    import bush_sentiment

    fake_pipeline = MagicMock(return_value=MagicMock(return_value=SAMPLE_CLASSIFIER_OUTPUT))
    monkeypatch.setattr(bush_sentiment, "hf_pipeline", fake_pipeline)

    bush_sentiment._load_model()

    fake_pipeline.assert_called_once()
    # Verify the call uses top_k=None (the v5.x kwarg), NOT return_all_scores.
    call = fake_pipeline.call_args
    assert call.kwargs.get("top_k") is None
    assert "return_all_scores" not in call.kwargs


def test_load_model_rejects_flat_list_shape(monkeypatch):
    """If transformers ever returns a flat list (no batch dim), fail loudly."""
    import bush_sentiment

    bad_output = [{"label": "joy", "score": 0.99}]  # missing outer batch dim
    fake_pipeline = MagicMock(return_value=MagicMock(return_value=bad_output))
    monkeypatch.setattr(bush_sentiment, "hf_pipeline", fake_pipeline)

    with pytest.raises(RuntimeError, match="unexpected shape"):
        bush_sentiment._load_model()


def test_load_model_rejects_empty_inner_list(monkeypatch):
    import bush_sentiment

    fake_pipeline = MagicMock(return_value=MagicMock(return_value=[[]]))
    monkeypatch.setattr(bush_sentiment, "hf_pipeline", fake_pipeline)

    with pytest.raises(RuntimeError, match="unexpected shape"):
        bush_sentiment._load_model()


def test_load_model_rejects_missing_keys(monkeypatch):
    """If a future shape change drops `score`, fail loudly."""
    import bush_sentiment

    bad_output = [[{"label": "joy"}]]  # no "score" key
    fake_pipeline = MagicMock(return_value=MagicMock(return_value=bad_output))
    monkeypatch.setattr(bush_sentiment, "hf_pipeline", fake_pipeline)

    with pytest.raises(RuntimeError, match="unexpected shape"):
        bush_sentiment._load_model()
