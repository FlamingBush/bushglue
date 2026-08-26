"""Unit tests for bush_t2v.cap_verse.

The corpus returns chunks that sometimes trail the scripture with study-bible
commentary and a chapter heading. bush-tts speaks the lot at ~0.55 s/word and
truncates at its own 60 s failsafe, so t2v caps the text before publishing it.
"""
import importlib
import sys

import pytest


def _reload_bush_t2v():
    """Reload bush_t2v so VERSE_MAX_WORDS picks up monkeypatched env."""
    if "bush_t2v" in sys.modules:
        del sys.modules["bush_t2v"]
    return importlib.import_module("bush_t2v")


# A real 88-word chunk observed on bush/pipeline/t2v/verse; it took bush-tts
# 54 s to speak, six seconds short of the failsafe.
LONG_CHUNK = (
    "Secret things to the Lord our God: things that are manifest, to\n"
    " us and to our children for ever, that we may do all the words of this\n"
    " law.\n Secret things, etc... As much as to say, secret things belong to, and\n"
    " are known to, God alone; our business must be to observe what he has\n"
    " revealed and manifested to us, and to direct our lives accordingly.\n"
    " Deuteronomy Chapter 30\n Great mercies are promised to the penitent: God's"
    " commandment is\n feasible. Life and death are set before them."
)


@pytest.fixture
def t2v(monkeypatch):
    monkeypatch.delenv("VERSE_MAX_WORDS", raising=False)
    return _reload_bush_t2v()


def test_short_verse_passes_through(t2v):
    text = "And the bush burned with fire and was not consumed."
    assert t2v.cap_verse(text) == text


def test_collapses_embedded_newlines(t2v):
    # Chunks break lines mid-sentence; espeak reads it either way but the
    # word count and sentence split need normalised whitespace.
    assert t2v.cap_verse("things that are manifest, to\n us") == (
        "things that are manifest, to us"
    )


def test_long_chunk_is_capped(t2v):
    capped = t2v.cap_verse(LONG_CHUNK)
    assert len(LONG_CHUNK.split()) > 40
    assert len(capped.split()) <= 40


def test_cap_stops_on_a_sentence_boundary(t2v):
    capped = t2v.cap_verse(LONG_CHUNK)
    # Not cut mid-clause: the kept text ends on real punctuation.
    assert capped.rstrip()[-1] in ".!?:"


def test_cap_drops_the_trailing_commentary(t2v):
    capped = t2v.cap_verse(LONG_CHUNK)
    assert "Deuteronomy Chapter 30" not in capped
    assert "As much as to say" not in capped


def test_explicit_max_words_overrides_default(t2v):
    text = " ".join(f"word{i}" for i in range(100))
    assert len(t2v.cap_verse(text, max_words=10).split()) <= 10


def test_zero_disables_the_cap(t2v):
    assert t2v.cap_verse(LONG_CHUNK, max_words=0) == " ".join(LONG_CHUNK.split())


def test_single_oversized_sentence_is_hard_cut(t2v):
    # No sentence boundary to stop at — cut at the word limit rather than
    # handing bush-tts the whole thing.
    text = " ".join(f"word{i}" for i in range(60)) + "."
    capped = t2v.cap_verse(text, max_words=10)
    assert len(capped.split()) == 10
    assert capped.endswith(".")


def test_env_var_sets_the_default(monkeypatch):
    monkeypatch.setenv("VERSE_MAX_WORDS", "5")
    mod = _reload_bush_t2v()
    assert mod.VERSE_MAX_WORDS == 5
    assert len(mod.cap_verse(LONG_CHUNK).split()) <= 5


# ── commentary stripping ────────────────────────────────────────────────────
# Measured over the 819 corpus chunks longer than 40 words: 8% carry an
# "etc.." lemma marker, 7% a "<Book> Chapter <N>" heading.

HEADING_CHUNK = (
    "In his days Hiel, of Bethel, built Jericho: in Abiram, his firstborn, he"
    " laid its foundations. 3 Kings Chapter 17 Elias shutteth up the heaven"
    " from raining. He is fed by ravens."
)


def test_strip_drops_the_lemma_commentary(t2v):
    out = t2v.strip_commentary(LONG_CHUNK)
    assert out.endswith("all the words of this law.")
    assert "etc..." not in out
    assert "As much as to say" not in out


def test_strip_drops_the_chapter_heading_and_summary(t2v):
    out = t2v.strip_commentary(HEADING_CHUNK)
    assert out == (
        "In his days Hiel, of Bethel, built Jericho: in Abiram, his firstborn,"
        " he laid its foundations."
    )


def test_strip_handles_a_numbered_book_name(t2v):
    assert "3 Kings Chapter 17" not in t2v.strip_commentary(HEADING_CHUNK)


def test_strip_leaves_clean_scripture_alone(t2v):
    text = "And the bush burned with fire and was not consumed."
    assert t2v.strip_commentary(text) == text


def test_strip_keeps_the_original_when_it_would_empty_the_verse(t2v):
    # A chunk that opens with the heading has no scripture to keep; leave it
    # to the word cap rather than publishing nothing.
    text = "Job Chapter 42 Job submits himself. God pronounces in his favour."
    assert t2v.strip_commentary(text) == text


def test_prepare_verse_strips_then_caps(t2v):
    out = t2v.prepare_verse(LONG_CHUNK)
    assert "Deuteronomy Chapter 30" not in out
    assert "etc..." not in out
    assert len(out.split()) <= 40


def test_prepare_verse_caps_when_stripping_is_not_enough(t2v):
    text = " ".join(f"word{i}" for i in range(90)) + "."
    assert len(t2v.prepare_verse(text).split()) <= 40


# ── bare "..." lemma, and abbreviations that fake a sentence end ────────────
# Measured over the 2772 corpus chunks longer than 25 words: 66 carry the
# "etc.." spelling, 159 the bare "...". Handling only the first left the
# larger half unstripped — observed live, the bush published a verse trailing
# "Spoken inconsiderately... If we discuss all Job's words (saith St."

DOTS_CHUNK = (
    "What can I answer, who hath spoken inconsiderately? I will lay my hand"
    " upon my mouth. Spoken inconsiderately... If we discuss all Job's words"
    " (saith St. Gregory), we shall find nothing impiously spoken."
)


def test_bare_dots_lemma_is_stripped(t2v):
    out = t2v.strip_commentary(DOTS_CHUNK)
    assert out == (
        "What can I answer, who hath spoken inconsiderately? I will lay my"
        " hand upon my mouth."
    )


def test_bare_dots_commentary_never_reaches_tts(t2v):
    out = t2v.prepare_verse(DOTS_CHUNK)
    assert "saith St." not in out
    assert "If we discuss" not in out


def test_trailing_ellipsis_is_not_a_lemma(t2v):
    # An ellipsis with nothing after it is just an ellipsis.
    text = "His body lay on the road, and the lion stood beside it..."
    assert t2v.strip_commentary(text) == text


def test_saint_abbreviation_is_not_a_sentence_end(t2v):
    # Without the guard the cap lands mid-citation and TTS reads "saith St."
    text = ("A reading attributed to saith St. Gregory and to many other"
            " fathers of the early church, recorded at length in the"
            " commentaries that follow this passage closely.")
    out = t2v.cap_verse(text, max_words=12)
    assert not out.rstrip().endswith("St.")


def test_lemma_stripping_survives_the_etc_form_too(t2v):
    # The original spelling must keep working alongside the new one.
    assert "As much as to say" not in t2v.strip_commentary(LONG_CHUNK)
