"""Voice direction (ADR-019): tags as positions over approved narration."""

from __future__ import annotations

import pytest

from videoforge_domain.timing import scene_spans, words_from_characters
from videoforge_domain.voice_direction import (
    AUDIO_TAGS,
    MAX_TAGS_PER_SCENE,
    DirectedScene,
    InvalidDirectionError,
    SceneText,
    Tag,
    compose,
    directed_scenes,
    parse_tagged_narration,
    strip_tag_characters,
    validate_direction,
)

ONE = SceneText("s1", 1, "Nobody knew what was inside.")
TWO = SceneText("s2", 2, "Until now!")


def _content(*tags_per_scene: list[dict[str, object]]) -> dict[str, object]:
    return {
        "scene_set_version_id": "v1",
        "scenes": [
            {"scene_id": scene.scene_id, "tags": tags}
            for scene, tags in zip((ONE, TWO), tags_per_scene, strict=True)
        ],
    }


class TestParseTaggedNarration:
    def test_tags_become_word_positions(self) -> None:
        tags = parse_tagged_narration(
            ONE.narration, "[whispers] Nobody knew what [short pause] was inside."
        )
        assert tags == (Tag(0, "whispers"), Tag(3, "short pause"))

    def test_a_trailing_tag_sits_after_the_last_word(self) -> None:
        assert parse_tagged_narration(TWO.narration, "Until now! [laughs]") == (
            Tag(2, "laughs"),
        )

    def test_a_tag_glued_to_a_word_still_counts(self) -> None:
        assert parse_tagged_narration(TWO.narration, "[excited]Until now!") == (
            Tag(0, "excited"),
        )

    def test_reworded_narration_is_refused_not_guessed(self) -> None:
        """The narration was approved word for word; a tag placed by fuzzy
        matching against a rewritten sentence lands in the wrong place."""
        assert parse_tagged_narration(TWO.narration, "[excited] Until now.") is None

    def test_unknown_tags_are_dropped_and_known_ones_kept(self) -> None:
        tags = parse_tagged_narration(TWO.narration, "[mysterious] Until [SAD] now!")
        assert tags == (Tag(1, "sad"),)

    def test_duplicates_collapse(self) -> None:
        tags = parse_tagged_narration(TWO.narration, "[sad] [sad] Until now!")
        assert tags == (Tag(0, "sad"),)


class TestValidateDirection:
    def test_canonical_content(self) -> None:
        content = validate_direction(
            _content([{"word": 0, "tag": "Whispers"}], []), (ONE, TWO)
        )
        assert content["scenes"][0] == {
            "scene_id": "s1",
            "scene_index": 1,
            "narration": ONE.narration,
            "tags": [{"word": 0, "tag": "whispers"}],
        }
        assert content["tag_count"] == 1

    def test_narration_comes_from_the_scene_set_not_the_request(self) -> None:
        """A client cannot change the words by sending different ones."""
        raw = _content([], [])
        raw["scenes"][0]["narration"] = "Something else entirely."  # type: ignore[index]
        content = validate_direction(raw, (ONE, TWO))
        assert content["scenes"][0]["narration"] == ONE.narration

    @pytest.mark.parametrize(
        ("tags", "message"),
        [
            ([{"word": 0, "tag": "mysterious"}], "not an available tag"),
            ([{"word": 99, "tag": "sad"}], "no word 99"),
            ([{"word": -1, "tag": "sad"}], "no word -1"),
            ([{"word": True, "tag": "sad"}], "word number"),
            ([{"word": "0", "tag": "sad"}], "word number"),
            ([{"word": 0, "tag": "sad"}] * (MAX_TAGS_PER_SCENE + 1), "at most"),
        ],
    )
    def test_bad_tags_are_refused(
        self, tags: list[dict[str, object]], message: str
    ) -> None:
        with pytest.raises(InvalidDirectionError, match=message):
            validate_direction(_content(tags, []), (ONE, TWO))

    def test_a_direction_for_another_scene_set_is_refused(self) -> None:
        moved = SceneText("s9", 2, TWO.narration)
        with pytest.raises(InvalidDirectionError, match="regenerate"):
            validate_direction(_content([], []), (ONE, moved))

    def test_a_scene_count_mismatch_is_refused(self) -> None:
        with pytest.raises(InvalidDirectionError, match="regenerate"):
            validate_direction(_content([], []), (ONE,))

    def test_every_offered_tag_is_accepted(self) -> None:
        tags = [{"word": 0, "tag": tag} for tag in AUDIO_TAGS]
        content = validate_direction(_content(tags, []), (ONE, TWO))
        assert [t["tag"] for t in content["scenes"][0]["tags"]] == list(AUDIO_TAGS)


class TestCompose:
    def test_no_tags_is_the_untagged_script_byte_for_byte(self) -> None:
        """What the voice stage sent before ADR-019, unchanged."""
        composed = compose([DirectedScene(ONE, ()), DirectedScene(TWO, ())])
        assert composed.text == f"{ONE.narration} {TWO.narration}"
        assert composed.tag_spans == ()

    def test_tags_are_inserted_before_their_word(self) -> None:
        composed = compose(
            [
                DirectedScene(ONE, (Tag(0, "whispers"), Tag(3, "short pause"))),
                DirectedScene(TWO, (Tag(0, "excited"), Tag(2, "laughs"))),
            ]
        )
        assert composed.text == (
            "[whispers] Nobody knew what [short pause] was inside. "
            "[excited] Until now! [laughs]"
        )
        for start, end in composed.tag_spans:
            assert composed.text[start] == "[" and composed.text[end - 1] == "]"
        assert composed.tag_count == 4

    def test_two_tags_on_one_word_keep_their_order(self) -> None:
        composed = compose([DirectedScene(TWO, (Tag(0, "sad"), Tag(0, "whispers")))])
        assert composed.text == "[sad] [whispers] Until now!"


class TestStripTagCharacters:
    @staticmethod
    def _aligned(text: str) -> tuple[list[str], list[float], list[float]]:
        """An alignment one-to-one with the request, as the mock returns."""
        chars = list(text)
        starts = [i * 0.1 for i in range(len(chars))]
        ends = [s + 0.1 for s in starts]
        return chars, starts, ends

    def test_tags_never_reach_the_captions(self) -> None:
        scenes = [
            DirectedScene(ONE, (Tag(0, "whispers"), Tag(3, "short pause"))),
            DirectedScene(TWO, (Tag(2, "laughs"),)),
        ]
        composed = compose(scenes)
        chars, starts, ends, method = strip_tag_characters(
            composed, *self._aligned(composed.text)
        )
        words = words_from_characters(chars, starts, ends)

        assert method == "offset"
        assert [w.text for w in words] == (ONE.narration + " " + TWO.narration).split()
        spans = scene_spans(words, [ONE.narration, TWO.narration])
        assert [len(span.words) for span in spans] == [5, 2]

    def test_timings_are_the_spoken_words_own(self) -> None:
        composed = compose([DirectedScene(TWO, (Tag(0, "excited"),))])
        chars, starts, _, _ = strip_tag_characters(
            composed, *self._aligned(composed.text)
        )
        # "Until" starts after "[excited] " — the tag's own audio time is a gap
        # before the first word, not stretched into it. The space after the
        # tag survives; word grouping already treats it as a separator.
        assert "".join(chars) == " Until now!"
        words = words_from_characters(chars, starts, starts)
        assert words[0].start_ms == round(len("[excited] ") * 100)

    def test_an_alignment_without_the_tags_falls_back_to_scanning(self) -> None:
        composed = compose([DirectedScene(TWO, (Tag(0, "excited"),))])
        chars, starts, ends, method = strip_tag_characters(
            composed, *self._aligned(TWO.narration)
        )
        assert method == "bracket_scan"
        assert "".join(chars) == TWO.narration

    def test_no_tags_passes_through(self) -> None:
        composed = compose([DirectedScene(TWO, ())])
        chars, _, _, method = strip_tag_characters(
            composed, *self._aligned(TWO.narration)
        )
        assert method == "none"
        assert "".join(chars) == TWO.narration

    def test_an_unclosed_bracket_swallows_nothing(self) -> None:
        composed = compose([DirectedScene(TWO, (Tag(0, "sad"),))])
        chars, _, _, _ = strip_tag_characters(composed, *self._aligned("[Until now!"))
        assert "".join(chars) == "[Until now!"


class TestDirectedScenes:
    def test_round_trip_from_stored_content(self) -> None:
        stored = validate_direction(
            _content([{"word": 1, "tag": "sad"}], []), (ONE, TWO)
        )
        scenes = directed_scenes(stored, (ONE, TWO))
        assert scenes[0].tags == (Tag(1, "sad"),)
        assert scenes[1].tags == ()
