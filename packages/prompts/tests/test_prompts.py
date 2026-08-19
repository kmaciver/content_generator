"""M2-05: versioned prompt templates.

Pure rendering, so everything here runs with no fixtures and no database. The
tests that matter are about *provenance* — the ref has to be able to tell two
different prompts apart, or the reproducibility chain of §10.3 rule 4 is
decorative.
"""

from __future__ import annotations

import pytest

import videoforge_prompts as prompts
from videoforge_prompts import (
    PromptTemplate,
    RenderedPrompt,
    UnknownTemplateError,
    available,
    render,
    render_block,
    template_ref,
)


class TestRefs:
    def test_a_ref_carries_name_version_and_digest(self) -> None:
        ref = template_ref("script")
        name, _, rest = ref.partition("@")
        version, _, digest = rest.partition("+")
        assert name == "script"
        assert version.isdigit()
        assert len(digest) == 8

    def test_editing_a_template_changes_the_ref_without_a_version_bump(self) -> None:
        """The whole reason the digest exists.

        A bare ``script/v1`` stays identical no matter how the prompt is
        edited, so every artifact ever generated claims the same provenance and
        "why does this script read like that?" has no answer. Two templates
        that differ by one word must not be able to claim they are the same.
        """
        original = PromptTemplate(name="script", version=1, source="be brief\n---\ngo")
        edited = PromptTemplate(name="script", version=1, source="be terse\n---\ngo")

        assert original.version == edited.version
        assert original.ref != edited.ref

    def test_identical_sources_agree(self) -> None:
        """Positive control: the digest must be *content*, not a nonce. If it
        varied per call, every version would look like a different prompt and
        the ref would be noise."""
        a = PromptTemplate(name="x", version=1, source="same\n---\nsame")
        b = PromptTemplate(name="x", version=1, source="same\n---\nsame")
        assert a.ref == b.ref


class TestRendering:
    def test_the_split_separates_system_from_user(self) -> None:
        result = render("script", topic="tides", target_seconds=50, research=None)
        assert isinstance(result, RenderedPrompt)
        assert "You write short-form educational video scripts" in result.system
        assert "tides" in result.user
        # The instruction block must not leak into the user turn: a system
        # prompt repeated as user content reads to the model as the operator
        # arguing with themselves.
        assert "You write short-form" not in result.user

    def test_an_undefined_variable_raises(self) -> None:
        """StrictUndefined is load-bearing.

        Jinja's default turns a missing topic into an empty string, so the
        prompt becomes "Write a script about: " and the model invents a
        subject. The artifact then looks completely normal and is about nothing
        the user asked for.
        """
        with pytest.raises(Exception, match="undefined|research"):
            render("script", topic="tides", target_seconds=50)

    def test_optional_sections_are_omitted_when_empty(self) -> None:
        """`research` is None until M2-09 wires the upstream stage in. The
        template must not ship a dangling "Research to work from:" header with
        nothing under it — the model treats that as an instruction to invent
        sources."""
        result = render("script", topic="tides", target_seconds=50, research=None)
        assert "Research to work from" not in result.user

    def test_a_provided_section_appears(self) -> None:
        result = render(
            "script", topic="tides", target_seconds=50, research="The moon pulls."
        )
        assert "Research to work from" in result.user
        assert "The moon pulls." in result.user

    def test_the_rendered_ref_matches_the_template(self) -> None:
        result = render("script", topic="tides", target_seconds=50, research=None)
        assert result.ref == template_ref("script")

    def test_an_unknown_template_lists_the_real_ones(self) -> None:
        with pytest.raises(UnknownTemplateError, match="script"):
            render("scrpit", topic="x")


class TestDiscovery:
    def test_templates_are_found_on_disk(self) -> None:
        assert "script" in available()

    def test_every_chat_template_has_both_sections(self) -> None:
        """A template with no separator renders entirely as a system prompt and
        sends an empty user turn — which most providers reject, but only at
        call time, in a worker, on someone's first generation.

        Scoped to chat templates since M3-03: an image prompt is one string,
        because that is what a diffusion provider takes.
        """
        for name in available():
            if name in prompts.BLOCK_TEMPLATES:
                continue
            rendered = render(name, **_context_for(name))
            assert rendered.system.strip(), name
            assert rendered.user.strip(), name

    def test_every_block_template_renders_to_one_section(self) -> None:
        """The other half of the invariant.

        Declared in ``BLOCK_TEMPLATES`` rather than sniffed from the source, so
        a chat template that *accidentally* lost its separator fails the test
        above instead of silently reclassifying itself as a block.
        """
        for name in sorted(prompts.BLOCK_TEMPLATES):
            rendered = render_block(name, **_context_for(name))
            assert rendered.text.strip(), name

    def test_block_templates_all_exist(self) -> None:
        """A name in ``BLOCK_TEMPLATES`` with no file behind it means a
        template was renamed or deleted and the declaration was not, so the
        block half of the invariant silently covers nothing."""
        assert set(available()) >= prompts.BLOCK_TEMPLATES

    def test_the_shipped_directory_is_inside_the_package(self) -> None:
        """It has to be packaged, not merely present in the checkout: the
        container installs a wheel, and a templates directory left behind by
        the build is a worker that boots and then cannot render anything."""
        assert prompts.TEMPLATES_DIR.is_dir()
        assert prompts.TEMPLATES_DIR.name == "templates"
        assert list(prompts.TEMPLATES_DIR.glob("*.jinja"))


def _context_for(name: str) -> dict[str, object]:
    """Every variable each template needs. Deliberately explicit — a template
    that grows a variable should fail here until someone decides what the
    callers pass."""
    # Annotated rather than inferred: without it mypy joins the entries'
    # differing value types down to `object` and the lookup stops type-checking.
    contexts: dict[str, dict[str, object]] = {
        "script": {"topic": "tides", "target_seconds": 50, "research": None},
        "research": {"topic": "tides", "target_seconds": 50},
        "caption": {
            "topic": "tides",
            "title": "Why the sea breathes",
            "script": "The moon pulls the water toward it.",
        },
        "scenes": {
            "title": "Tides",
            "script": "The moon pulls.",
            "target_ms": 50_000,
            "tolerance_ms": 7_500,
        },
        "prompt": {
            "index": 1,
            "total": 3,
            "visual_brief": "a beach at night",
            "narration": "The moon pulls.",
            "aspect": "9:16",
            "orientation": "vertical",
        },
        "image": {
            "style_block": "Medium: flat vector",
            "scene": "a beach at night",
            "character_block": "Pip —\n- head: a pale dome",
            "variable_block": "pose standing",
            "cast_block": "everyone has an oversized round head",
            "correction_block": "",
        },
    }
    return contexts[name]


class TestFrameConstraints:
    """Both templates that shape a scene image must refuse panels and text.

    Measured on the first live image run (2026-08-08). Two of five scenes came
    back as split panels and one carried mirror-written text — and in each case
    the *positive* prompt had asked for it, because ``prompts.generate`` had
    written "Split-screen composition divided by a vertical line down the
    center" and "an open notebook with 'budget' written on the cover page".

    The image template already said "No text ... anywhere in the image" and lost
    to the scene text that asked for some. So the refusal has to hold at both
    ends: the stage that *writes* briefs must not ask, and the stage that
    *renders* them must not comply.
    """

    def test_the_prompt_stage_is_told_not_to_ask(self) -> None:
        rendered = render(
            "prompt",
            index=1,
            total=3,
            visual_brief="contrast chores done with chores skipped",
            narration="Some kids do the dishes.",
            aspect="9:16",
            orientation="vertical",
        )
        # The *system* half may say "never", because it is read by an LLM that
        # understands instructions. What it must forbid is negations in the
        # model's own **output**, which becomes an image prompt verbatim.
        instructions = " ".join(rendered.system.lower().split())
        assert "write only what is present" in instructions
        assert "one continuous frame" in instructions
        assert "never by the words on them" in instructions
        # The frame's shape reaches the stage that composes for it — without
        # it, briefs describe square compositions the model then boxes.
        assert "the frame is 9:16 vertical" in instructions

    def test_the_image_frame_states_only_what_must_be_present(self) -> None:
        """**The positive block may not name a thing it does not want.**

        An image model reads the noun, not the instruction — the same rule that
        keeps ``style.avoid`` out of the positive block. Measured on
        2026-08-08: a frame block ending "No split screen, no panels, no
        dividing lines ... no inset or corner vignette" produced an image with
        a drawn border inset from the edges. Told not to divide the frame, the
        model drew a single panel instead.

        This is the third time the rule has been rediscovered (``avoid`` in the
        style block, the character's nose, now the border), so it is asserted
        rather than remembered.
        """
        # ``render_block``, not ``render``: the image frame is a block template
        # with no system/user split — it is composed into a provider request
        # rather than sent as a chat exchange.
        #
        # Every block empty, so what remains is the template's **own** wording
        # and nothing else. Caller-supplied blocks may legitimately say "no
        # hair" — that is a character trait, and it travels in the block whose
        # precedence is the whole point of this template.
        #
        # Whitespace-normalised: the template is wrapped prose, and a test that
        # broke when someone re-flowed a paragraph would be testing the line
        # width rather than the wording.
        frame = " ".join(
            render_block(
                "image",
                style_block="",
                scene="",
                character_block="",
                variable_block="",
                cast_block="",
                correction_block="",
            ).text.split()
        )

        assert "One continuous frame" in frame
        assert "all four edges" in frame
        assert "blank" in frame

        for banned in (" no ", "never", "without", "avoid", "not "):
            assert banned not in frame.lower(), (
                f"the frame's own wording names {banned!r}; prohibitions "
                "belong in the negative prompt, not the positive block"
            )
