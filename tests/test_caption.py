"""M5-01 — the Instagram caption stage.

Everything here is about :func:`normalise`, which is the part that can be wrong
*after* the model has done its job well. The prompt asks for a caption under
Instagram's limit with five to eight hashtags; a model usually complies, and
"usually" is not a contract. These are the bounds enforced on what came back.

Pure — no database, no provider. The stage body around it is one ``render``,
one ``llm_complete`` and one ``complete_generation``, all of which are the
shared machinery ``stages.py`` already tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from videoforge_workers.caption import (
    MAX_CAPTION_CHARACTERS,
    MAX_HASHTAGS,
    MAX_HOOK_CHARACTERS,
    PREVIEW_CHARACTERS,
    normalise,
)


def _normalise(**overrides: object) -> dict[str, Any]:
    raw: dict[str, object] = {
        "hook": "Why budgets fail",
        "caption": "Most budgets fail in week two. Here is the reason.",
        "hashtags": ["budgeting", "money", "personalfinance"],
    }
    raw.update(overrides)
    return normalise(raw, fallback_hook="a topic")


class TestCaption:
    def test_a_well_formed_answer_passes_through(self) -> None:
        result = _normalise()
        assert result["hook"] == "Why budgets fail"
        assert result["caption"].startswith("Most budgets fail")
        assert result["hashtags"] == ["budgeting", "money", "personalfinance"]

    def test_an_empty_caption_fails_the_stage(self) -> None:
        """The one case that raises rather than trims.

        A caption is the artifact. Trimming a too-long one still leaves
        something to review; an empty one leaves a reviewer looking at a blank
        panel and wondering whether the stage ran.
        """
        with pytest.raises(RuntimeError, match="no caption text"):
            _normalise(caption="   ")

    def test_an_overlong_caption_is_trimmed_not_rejected(self) -> None:
        """Instagram's hard limit. A caption 40 characters over is good copy
        and a bad count, and failing would discard a completion already paid
        for."""
        sentence = "This is a complete sentence about money. "
        long = sentence * 80
        assert len(long) > MAX_CAPTION_CHARACTERS

        result = _normalise(caption=long)
        assert len(result["caption"]) <= MAX_CAPTION_CHARACTERS

    def test_trimming_lands_on_a_sentence_end(self) -> None:
        """A caption that stops mid-thought reads as broken; one that stops a
        paragraph early reads as edited."""
        result = _normalise(caption="Money is odd. " * 300)
        assert result["caption"].endswith(".")

    def test_preview_is_what_shows_before_more(self) -> None:
        """Derived server-side so the 125-character rule has one home — the
        same reason M4-04's cue grouping is not reimplemented in TypeScript."""
        result = _normalise(caption="x" * 500)
        assert len(result["preview"]) == PREVIEW_CHARACTERS


class TestHook:
    def test_an_overlong_hook_is_cut_on_a_word(self) -> None:
        """This is the line typeset on the cover. "The surprising reason yo" is
        worse than a shorter hook."""
        result = _normalise(hook="The genuinely surprising reason that budgets fail")
        assert len(result["hook"]) <= MAX_HOOK_CHARACTERS
        assert not result["hook"].endswith(" ")
        assert result["hook"] in "The genuinely surprising reason that budgets fail"

    def test_a_missing_hook_falls_back_to_the_topic(self) -> None:
        """A cover with no words is a weaker cover and still a valid one; a
        stage that failed here would block the thumbnail on a field the model
        merely forgot."""
        assert _normalise(hook="")["hook"] == "a topic"


class TestHashtags:
    def test_tags_are_stored_bare(self) -> None:
        """Without the ``#``. The packager adds it, and bare is the form a
        person would type into a search box."""
        assert _normalise(hashtags=["#Money", "#budget"])["hashtags"] == [
            "money",
            "budget",
        ]

    def test_characters_instagram_drops_are_stripped(self) -> None:
        """``#dental-care`` is really ``#dental`` — the hyphen ends the tag.
        Stripping here means the stored tag is the tag that will exist."""
        assert _normalise(hashtags=["dental-care", "money!"])["hashtags"] == [
            "dentalcare",
            "money",
        ]

    def test_duplicates_collapse_after_normalisation(self) -> None:
        """A model returning ``#Budget`` and ``budget`` has returned one tag,
        and only lowercasing first makes that visible."""
        assert _normalise(hashtags=["Budget", "budget", "#BUDGET"])["hashtags"] == [
            "budget"
        ]

    def test_the_count_is_capped(self) -> None:
        """Instagram permits 30. Past roughly ten they stop being discovery and
        start reading as spam, so extra tags are dropped — the copy is fine,
        the model was just enthusiastic."""
        result = _normalise(hashtags=[f"tag{i}" for i in range(30)])
        assert len(result["hashtags"]) == MAX_HASHTAGS

    def test_a_non_list_is_no_tags_rather_than_an_error(self) -> None:
        """The same rule the research stage follows: a string where the schema
        asked for an array would otherwise be iterated character by character,
        producing a hashtag per letter."""
        assert _normalise(hashtags="budgeting money")["hashtags"] == []
