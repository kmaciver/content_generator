"""M3-03 and M3-05: style compilation and image prompt composition.

Pure functions, so no database and no provider. The properties under test are
determinism (the prompt is pinned into a snapshot, §10.3 rule 4) and the rule
that scene text cannot override immutable character traits.
"""

from __future__ import annotations

import pytest

from videoforge_prompts import UnknownTemplateError, render, render_block
from videoforge_prompts.image_prompt import (
    IMAGE_TEMPLATE,
    CharacterSpec,
    build_image_prompt,
)
from videoforge_prompts.style import CANONICAL_FIELDS, compile_style_block

PIP = CharacterSpec(
    name="Pip",
    immutable={
        "head": "a smooth pale dome, no hair",
        "eyes": "two black dots, no whites",
        "body": "a single rounded shape",
    },
    variable={"pose": "standing", "expression": "neutral"},
    never=("photorealism", "extra fingers"),
)

FLAT = {
    "medium": "flat vector illustration",
    "palette": ["#1B1B1B", "#F4EDE4", "#D96A4E"],
    "line": "no outlines",
    "shading": "flat fills, no gradients",
    "avoid": ["photorealism", "3d render", "photorealism"],
}


class TestStyleCompilation:
    def test_emits_canonical_fields_in_declared_order(self) -> None:
        """Not the mapping's order.

        A dict's order depends on how it was built — an editor form, a jsonb
        round-trip, a test literal — and three sources producing three
        different blocks for one style is the drift this prevents.
        """
        forwards = compile_style_block(FLAT)
        backwards = compile_style_block(dict(reversed(list(FLAT.items()))))
        assert forwards.block == backwards.block

        order = [
            line.split(":")[0] for line in forwards.block.splitlines() if ":" in line
        ]
        expected = [label for key, label in CANONICAL_FIELDS if key in FLAT]
        assert order == expected

    def test_lists_become_comma_joined(self) -> None:
        assert "#1B1B1B, #F4EDE4, #D96A4E" in compile_style_block(FLAT).block

    def test_avoid_never_appears_in_the_positive_block(self) -> None:
        """ "Avoid: photorealism" in a positive prompt is a well-known way to
        get photorealism — the model reads the noun, not the instruction."""
        spec = compile_style_block(FLAT)
        assert "3d render" not in spec.block
        assert "3d render" in spec.avoid

    def test_avoid_is_deduplicated_in_first_seen_order(self) -> None:
        """Order preserved rather than sorted: some providers weight a negative
        prompt positionally, and re-sorting would change its meaning."""
        assert compile_style_block(FLAT).avoid == ("photorealism", "3d render")

    def test_unknown_fields_are_carried_through_not_dropped(self) -> None:
        """An operator who adds a field should see it reach the model, not
        discover later that it was silently ignored."""
        spec = compile_style_block({"texture": "risograph grain"})
        assert "Texture: risograph grain" in spec.block

    def test_unknown_fields_are_sorted(self) -> None:
        a = compile_style_block({"zeta": "z", "alpha": "a"})
        b = compile_style_block({"alpha": "a", "zeta": "z"})
        assert a.block == b.block

    def test_empty_fields_compile_to_an_empty_spec(self) -> None:
        assert compile_style_block(None).is_empty
        assert compile_style_block({}).is_empty

    def test_a_malformed_value_degrades_rather_than_raising(self) -> None:
        """The operator is about to review the output anyway (§17). Failing the
        job because someone typed a number into a text field is worse."""
        assert "Mood: 7" in compile_style_block({"mood": 7}).block


class TestImmutableTraitsWin:
    def test_the_character_block_is_built_from_traits_alone(self) -> None:
        """Layer 1, and the one a test can actually prove: there is no code
        path by which scene text reaches the character block."""
        built = build_image_prompt(
            scene="Pip has long red hair blowing in the wind",
            character=PIP,
            style_fields=FLAT,
        )
        # The scene's claim is present as scene text...
        assert "long red hair" in built.prompt
        # ...and the authoritative block still says otherwise.
        assert "a smooth pale dome, no hair" in built.prompt

    def test_immutable_traits_come_last(self) -> None:
        """Layer 2. Models weight the end of a prompt more heavily, so the one
        thing that must not drift is the last thing read. Reordering the
        template silently weakens R7 while every other test still passes."""
        built = build_image_prompt(scene="A wide shot", character=PIP)
        assert built.prompt.index("Scene:") < built.prompt.index("smooth pale dome")

    def test_precedence_is_stated_explicitly(self) -> None:
        built = build_image_prompt(scene="A wide shot", character=PIP)
        assert "take precedence" in built.prompt

    def test_a_scene_touching_an_immutable_trait_is_recorded(self) -> None:
        """Layer 3. Recorded, never enforced — matching a word is not
        understanding a claim, and failing a job on a false positive would be
        worse than the drift it guards against."""
        built = build_image_prompt(scene="Pip's hair blows sideways", character=PIP)
        assert built.conflicts == ()  # 'hair' is a value, not a key

        built = build_image_prompt(scene="A close-up of Pip's eyes", character=PIP)
        assert "eyes" in built.conflicts

    def test_conflicts_do_not_fail_the_build(self) -> None:
        built = build_image_prompt(scene="Pip's eyes and body", character=PIP)
        assert built.prompt
        assert set(built.conflicts) == {"body", "eyes"}

    def test_unrevealing_keys_never_flag(self) -> None:
        """Trait keys like 'colour' appear in nearly every scene description.
        Flagging constantly trains a reviewer to ignore the signal."""
        character = CharacterSpec(name="X", immutable={"colour": "pale"})
        built = build_image_prompt(scene="a warm colour palette", character=character)
        assert built.conflicts == ()

    def test_variable_traits_are_offered_as_free_to_vary(self) -> None:
        built = build_image_prompt(scene="A wide shot", character=PIP)
        assert "Free to vary" in built.prompt
        assert "pose standing" in built.prompt


class TestDeterminism:
    def test_identical_inputs_produce_identical_bytes(self) -> None:
        a = build_image_prompt(scene="A wide shot", character=PIP, style_fields=FLAT)
        b = build_image_prompt(scene="A wide shot", character=PIP, style_fields=FLAT)
        assert a.prompt == b.prompt
        assert a.digest == b.digest

    def test_trait_order_does_not_change_the_prompt(self) -> None:
        """The same traits from a jsonb round-trip, an editor form and a test
        literal must produce one string, or the digest changes for reasons
        that have nothing to do with the character."""
        shuffled = CharacterSpec(
            name=PIP.name,
            immutable=dict(reversed(list(PIP.immutable.items()))),
            variable=dict(reversed(list(PIP.variable.items()))),
            never=PIP.never,
        )
        assert (
            build_image_prompt(scene="s", character=PIP).digest
            == build_image_prompt(scene="s", character=shuffled).digest
        )

    def test_the_digest_covers_the_negative_prompt_too(self) -> None:
        """Two generations with the same positive prompt and different
        negatives are different generations."""
        a = build_image_prompt(scene="s", character=PIP)
        b = build_image_prompt(scene="s", character=PIP, scene_negative="blur")
        assert a.prompt == b.prompt
        assert a.digest != b.digest

    def test_a_changed_scene_changes_the_digest(self) -> None:
        a = build_image_prompt(scene="one", character=PIP)
        b = build_image_prompt(scene="two", character=PIP)
        assert a.digest != b.digest


class TestNegativePrompt:
    def test_merges_character_style_and_scene_in_authority_order(self) -> None:
        """Character prohibitions lead: "never draw this character with a hat"
        outranks a scene's passing preference."""
        built = build_image_prompt(
            scene="s", character=PIP, style_fields=FLAT, scene_negative="motion blur"
        )
        assert built.negative_prompt == (
            "photorealism, extra fingers, 3d render, motion blur"
        )

    def test_deduplicates_across_sources(self) -> None:
        """`photorealism` is in both the character's `never` and the style's
        `avoid`; it should appear once."""
        built = build_image_prompt(scene="s", character=PIP, style_fields=FLAT)
        assert built.negative_prompt.count("photorealism") == 1

    def test_is_empty_when_nothing_forbids_anything(self) -> None:
        assert build_image_prompt(scene="s").negative_prompt == ""


class TestWithoutBranding:
    def test_a_scene_alone_still_builds(self) -> None:
        """`character` is optional because M3-06's admission check, not this
        function, refuses to generate without approved branding. Raising here
        would put one rule in two places."""
        built = build_image_prompt(scene="A wide shot of a valley")
        assert "A wide shot of a valley" in built.prompt
        assert "take precedence" not in built.prompt

    def test_the_blank_surfaces_instruction_is_always_present(self) -> None:
        """Captions are burned in later from word timestamps; a model that
        renders its own would double them.

        Phrased positively — "every ... label ... in frame is blank" rather
        than "no text" — since 2026-08-08. The positive block may not name a
        thing it does not want: an image model reads the noun, not the
        instruction, and the old "No split screen, no panels ..." wording
        produced a drawn border. The prohibition still exists, in the negative
        prompt where it bites.
        """
        assert "blank" in build_image_prompt(scene="s").prompt


class TestProvenance:
    def test_carries_the_template_ref(self) -> None:
        built = build_image_prompt(scene="s")
        assert built.template_ref.startswith(f"{IMAGE_TEMPLATE}@1+")

    def test_the_snapshot_carries_everything_needed_to_explain_the_image(
        self,
    ) -> None:
        built = build_image_prompt(scene="s", character=PIP, style_fields=FLAT)
        snapshot = built.snapshot(
            character_version_id="01ABC", style_version_id="01DEF"
        )
        assert snapshot["prompt"] == built.prompt
        assert snapshot["negative_prompt"] == built.negative_prompt
        assert snapshot["prompt_digest"] == built.digest
        assert snapshot["template_ref"] == built.template_ref
        # Version ids come from the caller: this module never sees a database.
        assert snapshot["character_version_id"] == "01ABC"


class TestRenderBlock:
    def test_rejects_a_chat_template(self) -> None:
        """Two strict contracts rather than one lenient one. A `render` that
        tolerated a missing user section would let a model answer a system
        prompt on its own."""
        with pytest.raises(ValueError, match="render_block"):
            render_block("script", topic="t", target_seconds=50, research="r")

    def test_render_rejects_a_block_template(self) -> None:
        with pytest.raises(ValueError, match="no user section"):
            render(
                IMAGE_TEMPLATE,
                style_block="",
                scene="s",
                character_block="",
                variable_block="",
                cast_block="",
                correction_block="",
            )

    def test_an_unknown_block_template_lists_the_valid_ones(self) -> None:
        with pytest.raises(UnknownTemplateError, match=IMAGE_TEMPLATE):
            render_block("nope")


class TestCast:
    """How **anyone else** in the frame is built (2026-08-08).

    Measured on the first multi-figure scene: a brief naming "a parent" and
    "two children" returned three figures with human proportions, hair and
    drawn faces — correctly rendered in the series' medium. The *rendering*
    transferred and the *construction* did not, because nothing described it.

    ``style`` said how things are drawn and ``character`` said who Pip is.
    Neither said how a person is built, so the model supplied ordinary humans.
    """

    CAST = "everyone has an oversized round head and thin stick limbs"

    def test_the_cast_rule_reaches_the_prompt(self) -> None:
        built = build_image_prompt(
            scene="a kitchen",
            character=PIP,
            style_fields={**FLAT, "cast": self.CAST},
        )
        assert self.CAST in built.prompt

    def test_it_is_read_after_the_character_not_in_the_style_block(self) -> None:
        """**Placement is the whole point.**

        The style block is emitted first and the scene follows it, so a cast
        rule left up there would be outranked by the scene's own "a parent
        with a warm smile". Models weight the end of a prompt — the same
        argument that puts the character block last.
        """
        built = build_image_prompt(
            scene="a kitchen with a parent at the table",
            character=PIP,
            style_fields={**FLAT, "cast": self.CAST},
        )
        assert built.prompt.index(self.CAST) > built.prompt.index("a kitchen")
        assert built.prompt.index(self.CAST) > built.prompt.index("flat vector")

    def test_it_never_appears_in_the_style_block(self) -> None:
        """A style field, but not a *style block* line.

        ``compile_style_block`` extracts it for position, the way it already
        extracts ``avoid`` for channel. A series that put it in the block would
        get the placement wrong for every image it ever generates.
        """
        spec = compile_style_block({**FLAT, "cast": self.CAST})
        assert spec.cast == self.CAST
        assert self.CAST not in spec.block
        assert "Cast:" not in spec.block

    def test_a_series_that_says_nothing_is_unaffected(self) -> None:
        """Optional, so no existing style has to be rewritten to keep working."""
        spec = compile_style_block(FLAT)
        assert spec.cast == ""
        built = build_image_prompt(scene="s", character=PIP, style_fields=FLAT)
        assert "Everyone else in the frame" not in built.prompt

    def test_it_changes_the_digest(self) -> None:
        """Two images differing only in the cast rule are different images, and
        the audit trail must not claim they came from the same prompt."""
        without = build_image_prompt(scene="s", character=PIP, style_fields=FLAT)
        with_cast = build_image_prompt(
            scene="s", character=PIP, style_fields={**FLAT, "cast": self.CAST}
        )
        assert without.digest != with_cast.digest
