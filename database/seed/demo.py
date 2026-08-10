"""The demo workspace, series, and two projects.

Two, not one, and that is the whole design of this module:

* **"Photosynthesis"** is *mid-review* — a rejected v1 and a v2 awaiting
  approval, with a real lineage between them. It exists so the review screen
  has something to show on first load: the version switcher, both status
  colours, and the approve/reject/regenerate/edit buttons all live.
* **"Plate tectonics"** is *empty* — a topic and nothing else. It exists so the
  "generate the first version" path is reachable by hand without deleting
  anything.

A seed that only produced the happy end state would leave both of those paths
untested by anyone eyeballing the UI, which is the main thing a seed is for.

Everything here is written through the same repositories and services the
application uses. Hand-written INSERTs would be faster and would quietly drift:
the seed would keep working after a schema change that broke the real write
path, which is the opposite of useful.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from videoforge_persistence.models import AppUser, Workspace
from videoforge_persistence.uow import UnitOfWork, unit_of_work
from videoforge_prompts.style import compile_style_block
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    ReviewDecisionKind,
    SubjectType,
    TransitionCause,
    UserRole,
    VersionOrigin,
)
from videoforge_shared.hashing import sha256_bytes

logger = logging.getLogger(__name__)

# Fixed ids. Not ULIDs in the generated sense — 26 characters of the right
# alphabet, deliberately readable, so a failing Playwright run or a bug report
# can name the exact row.
DEMO_WORKSPACE_ID = "01DEMOWORKSPACE00000000000"[:26]
DEMO_USER_ID = "01DEMOUSER0000000000000000"[:26]
DEMO_SERIES_ID = "01DEMOSERIES00000000000000"[:26]
DEMO_PROJECT_ID = "01DEMOPROJECT0000000000000"[:26]
DEMO_EMPTY_PROJECT_ID = "01DEMOPROJECTEMPTY00000000"[:26]
DEMO_CHARACTER_ID = "01DEMOCHARACTER0000000000"[:26]
DEMO_STYLE_ID = "01DEMOSTYLE000000000000000"[:26]

_RESEARCH_SUMMARY = (
    "Photosynthesis splits water using light energy, releasing oxygen as a "
    "by-product and using the hydrogen to build sugar from carbon dioxide."
)

_V1_TEXT = (
    "Plants are not really eating sunlight. That is the shorthand everyone "
    "learns, and it hides the interesting part. What actually happens is a "
    "controlled theft: chlorophyll strips an electron from water, and the "
    "whole rest of the process is the plant marching that electron downhill "
    "and taxing it at every step."
)

_V2_TEXT = (
    "Here is something most people get wrong about photosynthesis. Plants are "
    "not eating sunlight — they are stealing electrons.\n\n"
    "Chlorophyll absorbs a photon and uses that energy to rip an electron off "
    "a water molecule. The oxygen you are breathing right now is the leftover. "
    "It is waste.\n\n"
    "That stolen electron then falls through a chain of proteins, and at each "
    "step the plant skims off a little energy — the way a water wheel takes a "
    "cut of a river.\n\n"
    "What it buys with that energy is the ability to bend carbon dioxide into "
    "sugar. Air into food.\n\n"
    "And that is why nearly every meal you have ever eaten started as a "
    "photon and a stolen electron."
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    created: bool
    project_id: str
    empty_project_id: str


def seed_demo(engine: Engine) -> SeedResult:
    """Create the demo data. **Idempotent** — safe to run on every boot.

    Idempotency is checked on the workspace row rather than by catching a
    duplicate-key error: a seed that half-applied and then raised would leave a
    workspace with no project, and every later run would see the workspace and
    skip. Checking first, and doing all the work in one transaction, means the
    database is either fully seeded or untouched.
    """
    with unit_of_work(engine) as uow:
        if uow.workspaces.get(DEMO_WORKSPACE_ID) is not None:
            logger.info("demo data already present; skipping")
            return SeedResult(
                created=False,
                project_id=DEMO_PROJECT_ID,
                empty_project_id=DEMO_EMPTY_PROJECT_ID,
            )

        _build(uow)
        logger.info(
            "demo data created",
            extra={"project_id": DEMO_PROJECT_ID},
        )
        return SeedResult(
            created=True,
            project_id=DEMO_PROJECT_ID,
            empty_project_id=DEMO_EMPTY_PROJECT_ID,
        )


def _build(uow: UnitOfWork) -> None:
    # Flushed one at a time, in dependency order. Adding both and flushing once
    # relies on SQLAlchemy's topological sort, which needs a mapper-level
    # relationship to see the dependency — these models declare foreign keys
    # but no `relationship()`, so the sort has nothing to sort on and the
    # app_user INSERT can precede the workspace it references.
    uow.session.add(Workspace(id=DEMO_WORKSPACE_ID, name="Demo Workspace"))
    uow.flush()
    uow.session.add(
        AppUser(
            id=DEMO_USER_ID,
            workspace_id=DEMO_WORKSPACE_ID,
            email="demo@videoforge.local",
            display_name="Demo Reviewer",
            role=UserRole.OWNER,
        )
    )
    uow.flush()

    series = uow.series.create(
        workspace_id=DEMO_WORKSPACE_ID,
        title="Things You Half-Remember From School",
        # No style here: visual style is a series-scoped table with versions and
        # approval (ADR-016), seeded in M3. See `Series`.
        voice_preset={"tone": "curious, unhurried"},
        # Empty = all-manual (ApprovalPolicy.from_jsonb). Stated explicitly
        # because a demo that auto-approved would hide the gate the whole
        # product is built around.
        auto_approve_policy={},
        hashtag_template="#science #explained #{topic}",
    )
    series.id = DEMO_SERIES_ID
    uow.flush()

    _branding(uow)
    _seeded_project(uow)
    _empty_project(uow)


def _branding(uow: UnitOfWork) -> None:
    """An approved character and style for the demo series (M3-02, M3-05).

    Seeded so that a fresh ``make up`` can reach image generation without
    anyone hand-writing SQL — the admission check (M3-06) 409s a project whose
    series has no approved branding, and until the editor UI lands (M3-13)
    there is nowhere else to create one.

    The traits are deliberately **reductive**. Risk R7's finding is that
    consistency comes from a convention a diffusion model cannot get wrong: a
    pale circle with two dots is near-impossible to draw inconsistently, while
    a detailed face is near-impossible to draw consistently. A demo character
    with hair and clothing would model the wrong instinct for anyone copying
    it as a starting point.

    **Every element names its own colour, and that is not redundant with the
    style palette.** Measured against Gemini on 2026-08-07: with colours only in
    the palette, four scenes came back with the body terracotta, then black,
    then terracotta, then cream, and the limbs shuffling independently. The
    palette was obeyed perfectly — only the three colours ever appeared — but
    nothing said *which element gets which*, so the model reassigned them every
    scene. The palette declares what may be used; the traits declare where. A
    character whose colour scheme changes per scene is not a recurring
    character.

    The ``no ring or band`` clause is likewise from measurement, not
    imagination: the close-up grew a thick black ring around the head that read
    as hair or a hood. Close framing invites detail, and the traits have to
    refuse it by name.

    No reference sheets: those need real generated images (M3-04b). A character
    approved without them still works — image generation falls back to text
    alone — which is honest about what is built rather than seeding fake keys
    that point at nothing in object storage. The drift above is also the
    argument that reference sheets are **necessary rather than optional**.
    """
    character = uow.branding.add_character_version(
        DEMO_SERIES_ID,
        name="Pip",
        immutable_traits={
            "head": (
                "a smooth cream #F4EDE4 circle, no hair, no ears, "
                "no ring or band around it"
            ),
            "eyes": "two small black #141414 dots, no whites, no eyebrows",
            "body": "one rounded terracotta #D96A4E shape, always terracotta",
            "limbs": "thin black #141414 sticks",
            "scale": "the head is one third of total height",
        },
        variable_traits={
            "pose": "varies with the scene",
            "expression": "conveyed by posture, never by facial detail",
        },
    )
    character.id = DEMO_CHARACTER_ID
    uow.flush()
    uow.branding.approve_character(DEMO_CHARACTER_ID)

    fields: dict[str, Any] = {
        "medium": "flat vector illustration",
        "palette": ["#141414", "#F4EDE4", "#D96A4E", "#4E7AD9"],
        "line": "no outlines",
        "shading": "flat fills, no gradients",
        # Names the colour, not just "a single flat colour". The looser wording
        # was obeyed *literally and correctly* — and produced two cream
        # backgrounds followed by two black ones, which across hard cuts every
        # ~2.5s (§1.0.1) is a strobe rather than a style.
        "background": (
            "flat cream #F4EDE4, the same colour in every scene, no scenery"
        ),
        # The lower fifth is where burnt-in captions land (M4, from the voice
        # track's word timestamps). Reserving it in the *style* rather than
        # per scene is what keeps every episode's captions readable.
        "composition": (
            "the subject fills the middle of the frame; keep the lower fifth "
            "visually quiet"
        ),
        "detail": "radically reductive; shapes only",
        "avoid": ["photorealism", "3d render", "text", "gradients", "drop shadows"],
    }
    style = uow.branding.add_style_version(
        DEMO_SERIES_ID,
        name="Reductive Flat",
        fields=fields,
        # Compiled at seed time by the same function the API uses, so the
        # seeded row is byte-identical to one created through `POST /styles`.
        prompt_block=compile_style_block(fields).block,
    )
    style.id = DEMO_STYLE_ID
    uow.flush()
    uow.branding.approve_style(DEMO_STYLE_ID)


def _empty_project(uow: UnitOfWork) -> None:
    """A topic with no artifacts — the "generate the first version" path."""
    project = uow.projects.create(
        workspace_id=DEMO_WORKSPACE_ID,
        series_id=DEMO_SERIES_ID,
        topic="why tectonic plates move",
    )
    project.id = DEMO_EMPTY_PROJECT_ID
    uow.flush()


def _seeded_project(uow: UnitOfWork) -> None:
    """A project mid-review: approved research, then v1 rejected and v2 awaiting."""
    project = uow.projects.create(
        workspace_id=DEMO_WORKSPACE_ID,
        series_id=DEMO_SERIES_ID,
        topic="how photosynthesis actually works",
        title="Photosynthesis, actually",
    )
    project.id = DEMO_PROJECT_ID
    uow.flush()

    # **[M2-09]** Research first, approved. Script gained an upstream, so a
    # demo project with a script and no research shows an approved artifact the
    # DAG says cannot exist yet — and phase derivation would report RESEARCHING
    # over the top of it. Seeding the real shape keeps the demo honest and gives
    # the phase indicator (M2-13) something coherent to render.
    research = uow.artifacts.create(
        DEMO_PROJECT_ID, ArtifactKind.RESEARCH, state=ArtifactState.PENDING
    )
    uow.flush()
    research_v1 = uow.versions.add_version(
        research,
        origin=VersionOrigin.GENERATED,
        content_hash=sha256_bytes(_RESEARCH_SUMMARY.encode()),
        inline_content={
            "summary": _RESEARCH_SUMMARY,
            "key_facts": [
                "Chlorophyll reflects green, which is why leaves look green.",
                "The oxygen released comes from water, not carbon dioxide.",
            ],
            "surprising_angle": (
                "The plant is not eating sunlight; it is taking apart water."
            ),
            "misconception": "That plants get their mass from soil.",
        },
        prompt_template_ref="research@1+seeded0",
        provider_ref="mock",
        meta={"model": "mock-llm-v1", "seeded": True},
    )
    uow.reviews.record(
        artifact_version_id=research_v1.id,
        decision=ReviewDecisionKind.APPROVE,
        reviewer_id=DEMO_USER_ID,
        comment="Good angle — lead with the water.",
    )
    research.state = ArtifactState.APPROVED
    uow.projects.set_active_pointer(
        DEMO_PROJECT_ID, ArtifactKind.RESEARCH.value, research_v1.id
    )

    artifact = uow.artifacts.create(
        DEMO_PROJECT_ID, ArtifactKind.SCRIPT, state=ArtifactState.PENDING
    )
    uow.flush()

    v1 = uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        content_hash=sha256_bytes(_V1_TEXT.encode()),
        inline_content={"title": "Photosynthesis", "script": _V1_TEXT},
        prompt_template_ref="script/v1",
        provider_ref="mock",
        meta={"model": "mock-llm-v1", "seeded": True},
    )
    v2 = uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        content_hash=sha256_bytes(_V2_TEXT.encode()),
        inline_content={"title": "Photosynthesis, actually", "script": _V2_TEXT},
        prompt_template_ref="script/v1",
        provider_ref="mock",
        meta={"model": "mock-llm-v1", "seeded": True},
    )

    # v1 was rejected. Recorded as a real review_decision, so the status view
    # derives REJECTED the same way it would for a live rejection — a seed that
    # forced a status some other way would be demonstrating something that
    # cannot happen.
    uow.reviews.record(
        artifact_version_id=v1.id,
        decision=ReviewDecisionKind.REJECT,
        reviewer_id=DEMO_USER_ID,
        comment="Too abstract — open with something concrete.",
    )
    uow.comments.add(
        artifact_version_id=v1.id,
        body="The water-wheel image is good, keep it for the next pass.",
        author_id=DEMO_USER_ID,
    )

    artifact.state = ArtifactState.AWAITING_APPROVAL

    # The history a reviewer sees on the timeline. Written explicitly for the
    # same reason as above: these rows exist in production because services
    # write them, and the demo should look like production.
    for from_state, to_state, cause in (
        (None, ArtifactState.GENERATING.value, TransitionCause.SYSTEM),
        (
            ArtifactState.GENERATING.value,
            ArtifactState.AWAITING_APPROVAL.value,
            TransitionCause.JOB_SUCCEEDED,
        ),
        (
            ArtifactState.AWAITING_APPROVAL.value,
            ArtifactState.REJECTED.value,
            TransitionCause.REVIEW,
        ),
        (
            ArtifactState.REJECTED.value,
            ArtifactState.GENERATING.value,
            TransitionCause.SYSTEM,
        ),
        (
            ArtifactState.GENERATING.value,
            ArtifactState.AWAITING_APPROVAL.value,
            TransitionCause.JOB_SUCCEEDED,
        ),
    ):
        uow.audit.record_transition(
            subject_type=SubjectType.ARTIFACT,
            subject_id=artifact.id,
            from_state=from_state,
            to_state=to_state,
            cause=cause,
            actor_id=DEMO_USER_ID if cause is TransitionCause.REVIEW else None,
            correlation_id="seed",
        )

    uow.audit.record_event(
        event_type="artifact.version_created",
        subject_type=SubjectType.ARTIFACT,
        subject_id=artifact.id,
        payload={"version_id": v2.id, "version_no": v2.version_no},
        correlation_id="seed",
    )
    # NOT written to the outbox. These events already happened, notionally days
    # ago; publishing them on first boot would tell every future subscriber
    # that a script was just generated.
