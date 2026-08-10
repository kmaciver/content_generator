"""The daily spend cap (SADD §21.4, finding S10) — M3-11.

Pure: two numbers in, a verdict out. No session, no clock, no settings lookup.
The *policy* lives here so it can be tested with literals; reading what has
actually been spent is a query, and queries live in repositories.

**Why this is a pre-flight check rather than a post-hoc alarm.** The cap exists
to stop a regeneration loop, and a loop is only stoppable before the next call.
Checking after the fact would produce a very well-documented bill.

**The cap is deliberately not exact.** ``spent`` is a sum of estimates
(``providers.pricing``), and concurrent workers can each pass the check before
either records its usage — so a burst can overshoot by roughly one round of
in-flight calls. Closing that would mean holding a lock across a provider call,
which trades a bounded overshoot for a guaranteed serialisation of the whole
pipeline. For a guard rail whose job is "notice within a few dollars", the
overshoot is the better failure.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["BudgetExceededError", "check_budget", "remaining_budget"]


class BudgetExceededError(RuntimeError):
    """The daily estimated spend cap has been reached.

    A ``RuntimeError`` rather than a ``ProviderError``: no provider failed, and
    classifying it as one would put it through the retry middleware, which
    would retry a call that is being refused on purpose. The stage fails, the
    artifact goes FAILED, and an operator sees why.
    """

    def __init__(self, spent: Decimal, limit: Decimal, currency: str) -> None:
        super().__init__(
            f"daily provider spend estimate {spent:.2f} {currency} has reached "
            f"the limit of {limit:.2f} {currency}; no further calls will be "
            f"made today. Raise DAILY_COST_LIMIT or wait for the day to roll "
            f"over (UTC)."
        )
        self.spent = spent
        self.limit = limit
        self.currency = currency


def remaining_budget(spent: Decimal, limit: Decimal) -> Decimal:
    """What is left, floored at zero.

    Floored because a negative remaining budget is not a meaningful quantity to
    show anyone — over is over — and a caller rendering it would have to
    special-case the sign.
    """
    return max(Decimal(0), limit - spent)


def check_budget(spent: Decimal, limit: Decimal, *, currency: str = "USD") -> None:
    """Raise if the cap is reached. Returns ``None`` when there is room.

    ``limit <= 0`` means **no cap**, not "refuse everything". A zero limit
    reads as "unset" far more often than it reads as "spend nothing", and a
    deployment that accidentally blanked the value should keep working rather
    than halting every generation with a confusing error.

    The comparison is ``>=``: at exactly the limit the budget is spent, and
    letting one more call through because it happens to land on the boundary
    is the kind of off-by-one that only shows up in a bill.
    """
    if limit <= 0:
        return
    if spent >= limit:
        raise BudgetExceededError(spent, limit, currency)
