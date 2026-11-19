"""Human caption edits: approved exactly as typed, refused rather than rewritten."""

from __future__ import annotations

from typing import Any

import pytest

from videoforge_domain.post_copy import (
    MAX_CAPTION_CHARACTERS,
    MAX_HOOK_CHARACTERS,
    MAX_HUMAN_HASHTAGS,
    MAX_HUMAN_HOOK_CHARACTERS,
    PREVIEW_CHARACTERS,
    InvalidCopyError,
    validate_edited_caption,
)


def _edit(**overrides: object) -> dict[str, Any]:
    content: dict[str, object] = {
        "hook": "Money is not the first lesson",
        "caption": "My own words.",
        "hashtags": ["EducacaoFinanceira", "paisefilhos"],
        "preview": "the model's old opening line",
    }
    content.update(overrides)
    return validate_edited_caption(content)


class TestValidateEditedCaption:
    def test_the_preview_follows_the_edited_caption(self) -> None:
        """The bug: an edit kept the model's preview, so the review screen's
        "before more" line showed the old text and the edit looked unsaved."""
        long = "A" * (PREVIEW_CHARACTERS + 50)
        assert _edit(caption=long)["preview"] == long[:PREVIEW_CHARACTERS]
        assert _edit()["preview"] == "My own words."

    def test_what_was_typed_is_kept(self) -> None:
        result = _edit()
        assert result["hook"] == "Money is not the first lesson"
        assert result["caption"] == "My own words."
        # Casing survives: a reviewer's CamelCase tag is deliberate.
        assert result["hashtags"] == ["EducacaoFinanceira", "paisefilhos"]

    def test_hash_signs_and_exact_duplicates_are_dropped(self) -> None:
        result = _edit(hashtags=["#saúde", "saude", "SAÚDE", " ", "#saúde"])
        assert result["hashtags"] == ["saúde", "saude"]

    def test_a_hook_past_the_recommended_length_is_kept(self) -> None:
        """40 is advice (the editor warns); only 100 is refused."""
        hook = "x" * (MAX_HOOK_CHARACTERS + 20)
        assert _edit(hook=hook)["hook"] == hook

    def test_an_empty_hook_is_allowed(self) -> None:
        assert _edit(hook="  ")["hook"] == ""

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"caption": "  "}, "empty"),
            ({"caption": "x" * (MAX_CAPTION_CHARACTERS + 1)}, "2200"),
            ({"hook": "x" * (MAX_HUMAN_HOOK_CHARACTERS + 1)}, "100"),
            ({"hashtags": ["dental-care"]}, "dental-care"),
            ({"hashtags": "not a list"}, "list"),
            (
                {"hashtags": [f"t{i}" for i in range(MAX_HUMAN_HASHTAGS + 1)]},
                "30",
            ),
        ],
    )
    def test_limits_are_refused_not_rewritten(
        self, overrides: dict[str, object], message: str
    ) -> None:
        with pytest.raises(InvalidCopyError, match=message):
            _edit(**overrides)
