"""M4-06 — baked gain envelopes as an FFmpeg audio filter chain (S3).

Pure string generation, so these are cheap — but the strings are fed to a
parser that reports errors several filters away from the real problem, so most
of what is asserted here is syntax that was **verified against real ffmpeg**
on 2026-08-09: all three shapes below encoded successfully, and a 0 → −20 dB
envelope measured exactly 20.0 dB of attenuation across the ramp
(−21.1 dB before, −41.1 dB after, by ``volumedetect``).
"""

from __future__ import annotations

import pytest

from videoforge_workers.mixing import (
    GainStop,
    MixTrack,
    audio_filter_chain,
    gain_expression,
    to_amplitude,
)

_FLAT = (GainStop(at_ms=0, gain_db=0.0),)
_DUCK = (
    GainStop(at_ms=0, gain_db=-18.0),
    GainStop(at_ms=1200, gain_db=-18.0),
    GainStop(at_ms=2000, gain_db=-30.0),
)


class TestDecibels:
    @pytest.mark.parametrize(
        ("gain_db", "amplitude"),
        [(0.0, 1.0), (-6.0, 0.501187), (-20.0, 0.1), (6.0, 1.995262)],
    )
    def test_conversion(self, gain_db: float, amplitude: float) -> None:
        assert to_amplitude(gain_db) == pytest.approx(amplitude, abs=1e-6)


class TestGainExpression:
    def test_a_constant_envelope_is_a_plain_multiplier(self) -> None:
        """The only shape v1 produces (M4-07: no music). No expression, no
        per-frame evaluation, and readable in a graph someone is debugging."""
        assert gain_expression(_FLAT) == "volume=1.000000"

    def test_a_constant_non_zero_envelope_is_still_plain(self) -> None:
        assert gain_expression((GainStop(at_ms=0, gain_db=-6.0),)) == (
            "volume=0.501187"
        )

    def test_a_moving_envelope_becomes_a_per_frame_expression(self) -> None:
        expression = gain_expression(_DUCK)
        assert expression.startswith("volume=volume='")
        assert expression.endswith("':eval=frame")

    def test_commas_inside_the_expression_are_escaped(self) -> None:
        """**The one that bites.** A filtergraph separates filters on commas,
        so an unescaped comma inside an expression truncates the filter — and
        ffmpeg then reports a parse error about something several filters away
        from the real problem."""
        expression = gain_expression(_DUCK)
        body = expression.split("'")[1]
        assert "," not in body.replace("\\,", "")

    def test_the_envelope_holds_after_its_last_point(self) -> None:
        """The tail is the final value, not silence and not a ramp to zero."""
        body = gain_expression(_DUCK).split("'")[1]
        assert body.rstrip(")").endswith("0.031623")

    def test_a_descending_slope_reads_cleanly(self) -> None:
        """`0.125893+-0.117837` parses and has to be read twice."""
        assert "+-" not in gain_expression(_DUCK)

    def test_points_are_sorted_before_use(self) -> None:
        """The timeline validates ordering, but this module is also reachable
        from a hand-built track, and an unsorted envelope would produce a
        ramp running backwards through time."""
        forwards = gain_expression(_DUCK)
        assert gain_expression(tuple(reversed(_DUCK))) == forwards

    def test_two_points_at_one_time_are_a_step_not_a_crash(self) -> None:
        stops = (
            GainStop(at_ms=0, gain_db=0.0),
            GainStop(at_ms=1000, gain_db=0.0),
            GainStop(at_ms=1000, gain_db=-20.0),
        )
        assert "volume=volume='" in gain_expression(stops)

    def test_an_empty_envelope_is_refused(self) -> None:
        """It would leave the renderer to choose a gain, which is the decision
        S3 exists to move to compile time."""
        with pytest.raises(ValueError, match="needs a gain envelope"):
            gain_expression(())


class TestChain:
    def test_one_flat_track_is_the_v1_shape(self) -> None:
        assert audio_filter_chain(
            [MixTrack(input_index=1, start_ms=0, gain=_FLAT)], total_ms=98538
        ) == ("[1:a]volume=1.000000[a0];[a0]apad=whole_dur=98.538[aout]")

    def test_the_mix_is_padded_to_the_video(self) -> None:
        """**Not optional.** The video outlives the audio by `tail_ms`, and
        without padding the mix simply stops there — ffmpeg then either ends
        the encode early or leaves a stream shorter than the video, losing the
        held final frame that `tail_ms` exists to produce."""
        chain = audio_filter_chain(
            [MixTrack(input_index=1, start_ms=0, gain=_FLAT)], total_ms=98538
        )
        assert "apad=whole_dur=98.538" in chain

    def test_a_delayed_track_delays_every_channel(self) -> None:
        """Without `all=1`, adelay shifts only the first channel and a stereo
        bed arrives with its sides split."""
        chain = audio_filter_chain(
            [MixTrack(input_index=2, start_ms=500, gain=_FLAT)], total_ms=4000
        )
        assert "adelay=500:all=1" in chain

    def test_an_undelayed_track_gets_no_adelay(self) -> None:
        chain = audio_filter_chain(
            [MixTrack(input_index=1, start_ms=0, gain=_FLAT)], total_ms=4000
        )
        assert "adelay" not in chain

    def test_two_tracks_are_mixed_without_dropout_makeup(self) -> None:
        """amix's default *raises* the remaining inputs when one ends — which
        would silently undo the envelope the compiler baked, at the moment
        music ran out before the narration did."""
        chain = audio_filter_chain(
            [
                MixTrack(input_index=1, start_ms=0, gain=_FLAT),
                MixTrack(input_index=2, start_ms=0, gain=_DUCK),
            ],
            total_ms=4000,
        )
        assert "amix=inputs=2:duration=longest:dropout_transition=0" in chain

    def test_one_track_is_not_run_through_amix(self) -> None:
        chain = audio_filter_chain(
            [MixTrack(input_index=1, start_ms=0, gain=_FLAT)], total_ms=4000
        )
        assert "amix" not in chain

    def test_the_chain_ends_at_the_named_label(self) -> None:
        chain = audio_filter_chain(
            [MixTrack(input_index=1, start_ms=0, gain=_FLAT)],
            total_ms=4000,
            out_label="mixed",
        )
        assert chain.endswith("[mixed]")

    def test_no_tracks_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one track"):
            audio_filter_chain([], total_ms=4000)
