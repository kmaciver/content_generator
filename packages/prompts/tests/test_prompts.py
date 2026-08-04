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

    def test_every_template_has_both_sections(self) -> None:
        """A template with no separator renders entirely as a system prompt and
        sends an empty user turn — which most providers reject, but only at
        call time, in a worker, on someone's first generation."""
        for name in available():
            rendered = render(name, **_context_for(name))
            assert rendered.system.strip(), name
            assert rendered.user.strip(), name

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
    return {
        "script": {"topic": "tides", "target_seconds": 50, "research": None},
        "research": {"topic": "tides", "target_seconds": 50},
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
        },
    }[name]
