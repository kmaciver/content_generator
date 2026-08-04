"""M2-10: target video length (finding S11).

Pure, so no project row is needed. The cases worth having are the ones where a
plausible-looking config produces an absurd prompt.
"""

from __future__ import annotations

from typing import Any

from videoforge_domain.duration import (
    DEFAULT_TARGET_DURATION_MS,
    MAX_TARGET_DURATION_MS,
    MIN_TARGET_DURATION_MS,
    SETTINGS_KEY,
    duration_tolerance_ms,
    target_duration_ms,
)


class TestTargetDuration:
    def test_an_empty_project_gets_the_default(self) -> None:
        assert target_duration_ms(None) == DEFAULT_TARGET_DURATION_MS
        assert target_duration_ms({}) == DEFAULT_TARGET_DURATION_MS

    def test_a_configured_value_is_used(self) -> None:
        assert target_duration_ms({SETTINGS_KEY: 30_000}) == 30_000

    def test_seconds_mistaken_for_milliseconds_clamp_up(self) -> None:
        """The typo this guard exists for.

        ``target_duration_ms: 50`` reads perfectly naturally and asks for a
        fifty-*millisecond* video. Unclamped, the scenes stage would then be
        told to produce scenes summing to 50ms and would return something
        baffling — or one scene, which is worse because it looks deliberate.
        """
        assert target_duration_ms({SETTINGS_KEY: 50}) == MIN_TARGET_DURATION_MS

    def test_absurdly_long_clamps_down(self) -> None:
        assert target_duration_ms({SETTINGS_KEY: 10_000_000}) == MAX_TARGET_DURATION_MS

    def test_garbage_falls_back_rather_than_raising(self) -> None:
        """A bad number must not stop the pipeline.

        The target is a goal for a prompt, not a correctness constraint.
        Refusing to generate anything because someone typed "fifty" would be a
        worse outcome than generating a sensibly-sized video.
        """
        bad_values: list[Any] = ["fifty", None, [], {"nested": 1}]
        for bad in bad_values:
            assert target_duration_ms({SETTINGS_KEY: bad}) == DEFAULT_TARGET_DURATION_MS

    def test_a_numeric_string_is_accepted(self) -> None:
        """JSON round-trips and form posts both produce these."""
        assert target_duration_ms({SETTINGS_KEY: "45000"}) == 45_000


class TestTolerance:
    def test_the_window_scales_with_the_target(self) -> None:
        assert duration_tolerance_ms(50_000) == 7_500
        assert duration_tolerance_ms(20_000) == 3_000

    def test_the_window_is_wide_enough_to_be_useful(self) -> None:
        """Tight enough to catch six scenes or forty; loose enough that a model
        pacing sensibly is never punished for it."""
        target = DEFAULT_TARGET_DURATION_MS
        window = duration_tolerance_ms(target)
        assert 0.05 * target < window < 0.25 * target
