"""The daily spend cap's policy (M3-11, finding S10).

Pure arithmetic, so these run without a database — the property
``videoforge_domain`` exists to preserve.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from videoforge_domain.budget import (
    BudgetExceededError,
    check_budget,
    remaining_budget,
)


class TestCheckBudget:
    def test_under_the_limit_passes(self) -> None:
        check_budget(Decimal("4.99"), Decimal("10.00"))

    def test_at_the_limit_raises(self) -> None:
        """``>=``, not ``>``.

        At exactly the limit the budget is spent. Letting one more call through
        because it landed on the boundary is the off-by-one that only ever
        surfaces in a bill.
        """
        with pytest.raises(BudgetExceededError):
            check_budget(Decimal("10.00"), Decimal("10.00"))

    def test_over_the_limit_raises(self) -> None:
        with pytest.raises(BudgetExceededError):
            check_budget(Decimal("10.01"), Decimal("10.00"))

    @pytest.mark.parametrize("limit", [Decimal(0), Decimal("-1")])
    def test_non_positive_limit_means_no_cap(self, limit: Decimal) -> None:
        """Not "refuse everything".

        A blank ``DAILY_COST_LIMIT`` reads as unset far more often than it
        reads as "spend nothing", and a deployment that accidentally cleared it
        should keep working rather than failing every generation.
        """
        check_budget(Decimal("999999"), limit)

    def test_message_carries_the_configured_currency(self) -> None:
        """The number has to say what it is.

        ``cost_currency`` is a label over USD price tables, and an operator
        buying credits in CAD already asked once whether the cap meant their
        card statement. An error that omits the unit invites the question
        again at the worst moment.
        """
        with pytest.raises(BudgetExceededError) as caught:
            check_budget(Decimal("12"), Decimal("10"), currency="CAD")
        assert "CAD" in str(caught.value)

    def test_exception_carries_the_numbers(self) -> None:
        with pytest.raises(BudgetExceededError) as caught:
            check_budget(Decimal("12.50"), Decimal("10.00"))
        assert caught.value.spent == Decimal("12.50")
        assert caught.value.limit == Decimal("10.00")


class TestRemainingBudget:
    def test_reports_what_is_left(self) -> None:
        assert remaining_budget(Decimal("3.00"), Decimal("10.00")) == Decimal("7.00")

    def test_floors_at_zero(self) -> None:
        """A negative remaining budget is not a quantity worth rendering."""
        assert remaining_budget(Decimal("12.00"), Decimal("10.00")) == Decimal(0)
