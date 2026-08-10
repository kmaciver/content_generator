"""Pure compiler: approved artifacts -> Timeline JSON.

No I/O, fully unit-testable.
"""

from videoforge_timeline.compiler import (
    CompileOptions,
    Frame,
    Span,
    compile_timeline,
)
from videoforge_timeline.models import (
    SCHEMA_VERSION,
    AudioRole,
    AudioTrack,
    CaptionCue,
    Clip,
    GainPoint,
    Timeline,
    TimelineSource,
    TimelineVideo,
    Transition,
    TransitionKind,
)

__all__ = [
    "SCHEMA_VERSION",
    "AudioRole",
    "AudioTrack",
    "CaptionCue",
    "Clip",
    "CompileOptions",
    "Frame",
    "GainPoint",
    "Span",
    "Timeline",
    "TimelineSource",
    "TimelineVideo",
    "Transition",
    "TransitionKind",
    "compile_timeline",
]
