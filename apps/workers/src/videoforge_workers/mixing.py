"""Audio tracks + baked gain envelopes → an FFmpeg audio filter chain (M4-06).

This is the renderer side of **S3**. The timeline hands over an envelope that
is already resolved — dB values at absolute times — and this module's only job
is to *apply* it. Nothing here decides that narration is playing and music
should therefore drop; that decision was made at compile time, by something
with the whole video in front of it, and is visible in the stored artifact.

Engine-specific, like ``subtitles.py`` and for the same reason: ``volume``,
``adelay``, ``amix`` and ``apad`` are FFmpeg's words. The timeline says
"−18 dB from 0 ms"; this says how that is spelled.

**Music is out of v1** (M4-07), so every envelope this sees today has a single
point at 0 dB and every mix has one track. The multi-point path is still built
and tested, because the alternative is a renderer that reads ``gain[0]`` and
silently ignores the rest — which would be correct until the day music
arrives and wrong from then on, with nothing to catch it.

**Interpolation is linear in amplitude**, not in dB. Between two envelope
points the gain moves in a straight line through the multiplier rather than
through the decibel value, which keeps the expression free of ``pow`` and
therefore free of a second layer of comma-escaping. The two differ by a few
tenths of a dB mid-ramp on a typical duck — inaudible, and stated here rather
than discovered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "GainStop",
    "MixTrack",
    "audio_filter_chain",
    "gain_expression",
    "to_amplitude",
]


@dataclass(frozen=True, slots=True)
class GainStop:
    """One point of a baked envelope: dB at a time, relative to the track."""

    at_ms: int
    gain_db: float


@dataclass(frozen=True, slots=True)
class MixTrack:
    """One input stream and how to place it.

    ``input_index`` is the stream's position in the ffmpeg argv, which the
    caller owns — this module never builds a command line, only the chain
    between ``[n:a]`` and ``[aout]``.
    """

    input_index: int
    start_ms: int
    gain: tuple[GainStop, ...]


def audio_filter_chain(
    tracks: Sequence[MixTrack], *, total_ms: int, out_label: str = "aout"
) -> str:
    """The audio half of the filter graph, ending at ``[out_label]``.

    ``apad`` last, and it is not optional. The video outlives the audio by the
    timeline's ``tail_ms`` — 500 ms by default — and without padding the mix
    simply stops there. FFmpeg would then either end the whole encode early or
    leave a stream shorter than the video, depending on which output stream it
    decides is shortest; both are ways of losing the held final frame that
    ``tail_ms`` exists to produce.
    """
    if not tracks:
        raise ValueError("a mix needs at least one track")

    parts: list[str] = []
    labels: list[str] = []
    for position, track in enumerate(tracks):
        label = f"a{position}"
        parts.append(f"[{track.input_index}:a]{_track_chain(track)}[{label}]")
        labels.append(label)

    if len(labels) == 1:
        mixed = labels[0]
    else:
        # `dropout_transition=0`: amix's default is to *raise* the gain of the
        # remaining inputs when one ends, which would silently undo the
        # envelope the compiler baked the moment music ran out before the
        # narration did.
        joined = "".join(f"[{label}]" for label in labels)
        parts.append(
            f"{joined}amix=inputs={len(labels)}:duration=longest"
            f":dropout_transition=0[amix]"
        )
        mixed = "amix"

    parts.append(f"[{mixed}]apad=whole_dur={_seconds(total_ms)}[{out_label}]")
    return ";".join(parts)


def _track_chain(track: MixTrack) -> str:
    """Delay then gain, for one track."""
    filters: list[str] = []
    if track.start_ms > 0:
        # `all=1` so every channel is delayed. Without it adelay shifts only
        # the first channel and a stereo bed arrives with its sides split.
        filters.append(f"adelay={track.start_ms}:all=1")
    filters.append(gain_expression(track.gain))
    return ",".join(filters)


def gain_expression(gain: Sequence[GainStop]) -> str:
    """One envelope, as a ``volume`` filter.

    A constant envelope becomes a plain multiplier — no expression, no
    per-frame evaluation. That is the only case v1 produces, and it is worth
    keeping cheap and readable in the graph an operator might have to debug.

    Anything with movement becomes an expression in ``t`` with
    ``eval=frame``. **Commas inside it are escaped**: a filtergraph separates
    filters on commas, so an unescaped one inside an expression truncates the
    filter and FFmpeg reports a parse error about something several filters
    away from the real problem.
    """
    if not gain:
        raise ValueError("a track needs a gain envelope")

    stops = sorted(gain, key=lambda stop: stop.at_ms)
    if len({stop.gain_db for stop in stops}) == 1:
        return f"volume={to_amplitude(stops[0].gain_db):.6f}"

    expression = _piecewise(stops)
    return f"volume=volume='{expression}':eval=frame"


def _piecewise(stops: Sequence[GainStop]) -> str:
    """Nested ``if(lt(t,…))`` — held before the first stop, held after the
    last, straight lines in between."""
    amplitudes = [to_amplitude(stop.gain_db) for stop in stops]
    seconds = [stop.at_ms / 1000 for stop in stops]

    # Built from the end backwards so each segment can name the expression for
    # everything after it: the tail is the final held value.
    expression = f"{amplitudes[-1]:.6f}"
    for index in range(len(stops) - 2, -1, -1):
        start_t, end_t = seconds[index], seconds[index + 1]
        start_a, end_a = amplitudes[index], amplitudes[index + 1]
        span = end_t - start_t
        if span <= 0:
            # Two stops at one time is a step, not a ramp. The timeline
            # forbids it, but a caller that built tracks by hand should get a
            # step rather than a division by zero.
            segment = f"{start_a:.6f}"
        else:
            slope = (end_a - start_a) / span
            # `{:+.6f}` rather than a literal `+`: a descending ramp otherwise
            # renders as `0.125893+-0.117837`, which FFmpeg accepts and a
            # human debugging the graph has to read twice.
            segment = f"({start_a:.6f}{slope:+.6f}*(t-{start_t:.3f}))"
        expression = f"if(lt(t\\,{end_t:.3f})\\,{segment}\\,{expression})"

    # Before the first stop, hold the first value.
    first_t = seconds[0]
    if first_t > 0:
        expression = f"if(lt(t\\,{first_t:.3f})\\,{amplitudes[0]:.6f}\\,{expression})"
    return expression


def to_amplitude(gain_db: float) -> float:
    """dB → linear multiplier. 0 dB is 1.0, −6 dB is roughly a half."""
    return float(10 ** (gain_db / 20))


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"
