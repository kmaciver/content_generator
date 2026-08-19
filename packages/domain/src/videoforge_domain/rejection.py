"""Why a version was rejected, and what to tell the model next time (M3-10).

**The gap this closes.** Rejecting already worked, and so did regenerating a
selected scene (§12.5). What was missing is the bit between them: a rejection
recorded only as free text is a note to a human, and the next generation runs
against exactly the prompt that just failed. The reviewer says "his head went
wrong again", clicks Regenerate, and the model is told nothing it did not
already know.

A **structured** reason is machine-readable, so it can become a correction the
next prompt actually carries — and it is countable, which free text is not.
"Character drift on 6 of 20 scenes" is a fact you can act on; twenty sentences
are not.

**The taxonomy is grounded, not invented.** Every entry below is a failure this
project actually produced against Gemini between 2026-08-07 and 08-08, and the
correction text is the wording that fixed it where a fix was found. A category
nobody has hit yet is a category nobody will pick correctly.

**Two channels, again.** A reason contributes a *positive* correction line and,
where the failure is an unwanted thing, extra negative terms — because the
lesson this codebase has now relearned three times is that an image model reads
nouns, not instructions, so a prohibition belongs in the negative prompt and
never in the positive block.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from videoforge_shared.enums import ArtifactKind

__all__ = [
    "CORRECTIONS",
    "Correction",
    "RejectionReason",
    "build_correction",
    "reasons_for",
]


class RejectionReason(StrEnum):
    """What was wrong, from a fixed vocabulary.

    Deliberately short. A taxonomy a reviewer has to read twice is one they
    will answer with ``OTHER`` every time, and then it records nothing.
    """

    #: The character does not match the approved sheets — proportions, colour
    #: assignment, added features. The R7 failure mode.
    CHARACTER_DRIFT = "character_drift"
    #: Rendered in the wrong medium, palette or line treatment for the series.
    STYLE_DRIFT = "style_drift"
    #: Split panels, borders, letterboxing, a square subject in a tall frame.
    COMPOSITION = "composition"
    #: Legible or garbled writing anywhere in frame.
    TEXT_ARTIFACTS = "text_artifacts"
    #: Wrong number of limbs, missing feet, impossible joints.
    ANATOMY = "anatomy"
    #: People, animals or props that the brief never asked for.
    EXTRA_SUBJECTS = "extra_subjects"
    #: Technically fine, but not what the scene describes.
    OFF_BRIEF = "off_brief"
    #: Smearing, mush, artefacts — a bad roll rather than a bad instruction.
    QUALITY = "quality"
    #: The comment carries it. Present so the list stays honest rather than
    #: forcing a wrong category, and worth watching: a high ``OTHER`` rate
    #: means the vocabulary above is missing something.
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Correction:
    """What a reason contributes to the next attempt."""

    #: A positive instruction. Never names the unwanted thing.
    guidance: str
    #: Extra negative-prompt terms, where the failure is a thing to suppress.
    avoid: tuple[str, ...] = ()


#: Reason → what to say next time.
#:
#: The wording is the wording that worked. ``COMPOSITION`` and
#: ``TEXT_ARTIFACTS`` in particular repeat the phrasing that fixed the split
#: panels and the mirror-written labels on 2026-08-08, rather than a fresh
#: paraphrase that would have to be re-proven.
#:
#: Each ``guidance`` is a **pure corrective instruction**, in the imperative.
#: The template already says a previous attempt was rejected, so repeating that
#: per reason adds words without information — and a phrasing like "the last
#: attempt contained writing" would put the very noun the ``avoid`` list is
#: suppressing back into the positive prompt. A test asserts that no guidance
#: contains any of its own avoid terms; it caught exactly that on the first run.
CORRECTIONS: dict[RejectionReason, Correction] = {
    RejectionReason.CHARACTER_DRIFT: Correction(
        guidance=(
            "Follow the character block below exactly — every named colour on "
            "every named element, and the head at the stated proportion."
        ),
    ),
    RejectionReason.STYLE_DRIFT: Correction(
        guidance=(
            "Match the medium, palette, line and shading described above " "precisely."
        ),
        avoid=("photographic", "3d render", "digital airbrush", "gradients"),
    ),
    RejectionReason.COMPOSITION: Correction(
        guidance=(
            "One continuous frame: one moment, one place, one camera. The "
            "scene runs to all four edges of the image and bleeds off them."
        ),
        avoid=(
            "split screen",
            "multiple panels",
            "divided frame",
            "border",
            "framed border",
            "margin",
            "letterbox",
            "vignette",
        ),
    ),
    RejectionReason.TEXT_ARTIFACTS: Correction(
        guidance=(
            "Every book, sign, screen, receipt, label and packet in frame is "
            "completely blank."
        ),
        avoid=("text", "letters", "numbers", "writing", "handwriting", "labels"),
    ),
    RejectionReason.ANATOMY: Correction(
        guidance=(
            "Each figure has exactly two arms and two legs, all four visible "
            "and correctly attached to the body."
        ),
        avoid=("extra limbs", "missing limbs", "malformed hands", "fused limbs"),
    ),
    RejectionReason.EXTRA_SUBJECTS: Correction(
        guidance="Draw only what the scene names, and nothing else.",
        avoid=("extra people", "crowd", "background characters", "clutter"),
    ),
    RejectionReason.OFF_BRIEF: Correction(
        guidance=(
            "Read the scene description again and show exactly the moment it "
            "describes."
        ),
    ),
    RejectionReason.QUALITY: Correction(
        guidance="Draw it cleanly, with crisp shapes and clear edges.",
        avoid=("blurry", "smeared", "mushy", "artefacts", "low detail"),
    ),
    # No entry for OTHER on purpose: the reviewer's own words are the guidance,
    # and inventing a generic sentence would dilute them.
}


def reasons_for(kind: ArtifactKind) -> tuple[RejectionReason, ...]:
    """Which reasons a reviewer may pick for this kind of artifact.

    **Every reason above describes a picture.** "Anatomy" and "Text in image"
    are not things a narration, a script or a timeline can be wrong about, and
    the review screen offered all nine on every rejectable artifact — so a
    reviewer rejecting a voice take was asked to choose between failure modes
    none of which could apply. A vocabulary that does not fit is worse than no
    vocabulary: it gets answered with ``OTHER``, or worse, answered wrongly and
    counted.

    **Kinds without a grounded taxonomy get an empty tuple**, and their review
    falls back to the free-text comment alone. That is deliberate, and it is
    this module's own rule applied honestly: the image list exists because
    those nine failures were *observed* between 2026-08-07 and 08-08. Nothing
    has yet rejected a narration, so any voice vocabulary written today would
    be guesswork — and a guessed category is one nobody picks correctly.

    Voice is the obvious next candidate, and it needs two things first: real
    rejections to draw the categories from, and somewhere for the resulting
    correction to *go*. ``voice.generate`` currently consumes no correction, so
    a structured reason would be recorded and change nothing.
    """
    return _REASONS_BY_KIND.get(kind, ())


#: The one place the mapping lives. Image artifacts only, for now — see
#: :func:`reasons_for`.
_REASONS_BY_KIND: dict[ArtifactKind, tuple[RejectionReason, ...]] = {
    ArtifactKind.IMAGE: tuple(RejectionReason),
}


def build_correction(
    reasons: Sequence[str] | None, comment: str | None = None
) -> Correction:
    """Merge a rejection's reasons into one correction for the next attempt.

    Order follows :class:`RejectionReason`'s declaration rather than the order
    the reviewer happened to click, so the same set of reasons always produces
    byte-identical text — the prompt is pinned into a snapshot (§10.3 rule 4),
    and a block that reshuffled itself would make two identical regenerations
    look different in the audit trail.

    The reviewer's comment goes **last**, where a model weights it most, and is
    included whatever the reasons are: a human who took the trouble to write a
    sentence has said something the taxonomy cannot.

    Unknown strings are ignored rather than raising. This reads rows written by
    an older build, and a reason retired in a later version must not make an
    old artifact impossible to regenerate.
    """
    chosen = set(reasons or ())
    lines: list[str] = []
    avoid: dict[str, None] = {}

    for reason in RejectionReason:
        if reason.value not in chosen:
            continue
        correction = CORRECTIONS.get(reason)
        if correction is None:
            continue
        lines.append(correction.guidance)
        for term in correction.avoid:
            avoid.setdefault(term, None)

    if note := (comment or "").strip():
        lines.append(f"The reviewer said: {note}")

    return Correction(guidance="\n".join(lines), avoid=tuple(avoid))


def is_known(reason: str) -> bool:
    """Whether a stored string is still part of the vocabulary."""
    return reason in {member.value for member in RejectionReason}


def known(reasons: Iterable[str]) -> tuple[str, ...]:
    """Filter stored reasons to the ones this build understands."""
    return tuple(reason for reason in reasons if is_known(reason))
