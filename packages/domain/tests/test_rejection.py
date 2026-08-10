"""M3-10: structured rejection reasons become the next attempt's correction.

Pure functions — no database, no provider. What matters here is that the same
reasons always compile to the same text (the prompt is pinned into a snapshot,
§10.3 rule 4) and that a prohibition lands in the negative channel rather than
the positive block.
"""

from __future__ import annotations

from videoforge_domain.rejection import (
    CORRECTIONS,
    RejectionReason,
    build_correction,
    known,
)


class TestVocabulary:
    def test_every_reason_but_other_has_a_correction(self) -> None:
        """A reason a reviewer can pick that changes nothing about the next
        attempt is a control that lies about what it does."""
        for reason in RejectionReason:
            if reason is RejectionReason.OTHER:
                continue
            assert reason in CORRECTIONS, reason

    def test_other_deliberately_has_none(self) -> None:
        """The reviewer's own words are the guidance; a generic sentence
        alongside them would only dilute what they wrote."""
        assert RejectionReason.OTHER not in CORRECTIONS

    def test_no_correction_names_what_it_forbids_in_its_guidance(self) -> None:
        """**The rule this codebase keeps rediscovering.**

        An image model reads the noun, not the instruction — ``style.avoid`` is
        kept out of the positive block for this reason, and a frame template
        that said "no split screen, no panels" produced a drawn border
        (2026-08-08). A correction's guidance is positive prose; the things to
        suppress travel in ``avoid``.
        """
        for reason, correction in CORRECTIONS.items():
            for term in correction.avoid:
                assert term not in correction.guidance, (reason, term)


class TestBuildCorrection:
    def test_no_reasons_produces_nothing(self) -> None:
        """The ordinary first generation must cost no extra prompt text."""
        correction = build_correction(None)
        assert correction.guidance == ""
        assert correction.avoid == ()

    def test_a_reason_contributes_guidance_and_negatives(self) -> None:
        correction = build_correction([RejectionReason.TEXT_ARTIFACTS.value])
        assert "blank" in correction.guidance
        assert "writing" in correction.avoid

    def test_order_follows_the_enum_not_the_click_order(self) -> None:
        """Byte-identical text for the same set, whichever order the reviewer
        ticked the boxes — the digest pins this prompt into an audit trail, and
        a block that reshuffled itself would make two identical regenerations
        look different."""
        forwards = build_correction(
            [RejectionReason.CHARACTER_DRIFT.value, RejectionReason.COMPOSITION.value]
        )
        backwards = build_correction(
            [RejectionReason.COMPOSITION.value, RejectionReason.CHARACTER_DRIFT.value]
        )
        assert forwards.guidance == backwards.guidance
        assert forwards.avoid == backwards.avoid

    def test_duplicate_negatives_are_deduplicated(self) -> None:
        correction = build_correction(
            [RejectionReason.COMPOSITION.value, RejectionReason.STYLE_DRIFT.value]
        )
        assert len(correction.avoid) == len(set(correction.avoid))

    def test_the_comment_comes_last(self) -> None:
        """Where a model weights it most. A human who wrote a sentence has said
        something the taxonomy cannot."""
        correction = build_correction(
            [RejectionReason.CHARACTER_DRIFT.value], "the ears are back"
        )
        assert correction.guidance.endswith("The reviewer said: the ears are back")

    def test_a_comment_alone_still_produces_guidance(self) -> None:
        """Rejecting with prose and no category must not silently drop it."""
        correction = build_correction([], "too dark")
        assert "too dark" in correction.guidance

    def test_unknown_reasons_are_ignored_rather_than_raising(self) -> None:
        """**Rows outlive the vocabulary that wrote them.**

        This reads ``review_decision`` rows written by earlier builds. A reason
        retired in a later version must not make an old artifact impossible to
        regenerate — which is what raising here would mean.
        """
        correction = build_correction(
            ["a_reason_from_2027", RejectionReason.ANATOMY.value]
        )
        assert "two arms and two legs" in correction.guidance
        assert "a_reason_from_2027" not in correction.guidance

    def test_known_filters_to_the_current_vocabulary(self) -> None:
        assert known(["quality", "nonsense"]) == ("quality",)
