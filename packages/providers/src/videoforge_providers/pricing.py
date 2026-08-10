"""Cost estimation — the numbers behind the S10 daily cap (M3-11).

Until this module existed, ``Usage.unit_cost_estimate`` was ``0.0`` on every
call in the system. ``ProviderUsageRepository.spend_since`` summed those zeros,
and ``daily_cost_limit`` therefore capped nothing at all: the guard was fully
wired, fully tested, and enforcing a total that could never rise.

That was survivable while every stage was a cheap text call. It stops being
survivable at M3-04, where a single character-reference run generates 4–8
images and a dissatisfied operator runs it again.

**These are estimates, not invoices.** The field is named ``estimate`` for that
reason (§11's `Money` note, and ``CoreSettings.cost_currency``). Three things
make an exact figure impossible here, and all three are fine for a guard rail:

* prompt caching and batch discounts are applied vendor-side and are invisible
  to the adapter;
* a failed call that consumed tokens before erroring is not metered at all;
* vendors round and bill in their own currency, on their own cycle.

The point of the number is to notice a regeneration loop within a few dollars,
not to reconcile against a statement.

**Prices are USD and are transcribed by hand.** ``cost_currency`` is a *label*
applied to these figures, never a conversion — changing it without changing
this table relabels the numbers and makes them wrong. Verify against the
vendor's pricing page when adding a model; a table that silently rots is worse
than no table, which is why an unknown model logs rather than guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

__all__ = [
    "IMAGE_PRICES",
    "LLM_PRICES",
    "ImagePrice",
    "TokenPrice",
    "estimate_image_cost",
    "estimate_llm_cost",
]

#: Tokens are priced per million; images per image. Keeping the units in the
#: type rather than in a comment is what stops a later contributor entering a
#: per-thousand figure into a per-million field.
_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """USD per **million** tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclass(frozen=True, slots=True)
class ImagePrice:
    """USD per image produced."""

    per_image: Decimal


#: Keyed by model id. Lookup is longest-prefix, so a dated id
#: (``claude-sonnet-5-20260101``) resolves against its family without an entry
#: per release — vendors publish prices per family and date the ids.
#:
#: **Unverified as of 2026-08-05.** These are the published list prices to the
#: best of the author's knowledge and have not been checked against an invoice.
#: Treat the *mechanism* as delivered and the *figures* as needing one look at
#: the pricing page before anyone relies on the cap being exact.
LLM_PRICES: dict[str, TokenPrice] = {
    "claude-sonnet": TokenPrice(Decimal("3.00"), Decimal("15.00")),
    "claude-opus": TokenPrice(Decimal("15.00"), Decimal("75.00")),
    "claude-haiku": TokenPrice(Decimal("0.80"), Decimal("4.00")),
    # The mock spends nothing, and saying so explicitly keeps it out of the
    # "unknown model" warning path that real typos need to stay visible in.
    "mock-llm": TokenPrice(Decimal(0), Decimal(0)),
}

#: Per **image**, not per token. Image models bill per output picture, and the
#: token counts Gemini also reports describe the prompt rather than the cost.
#:
#: **The Gemini figure is a placeholder and is almost certainly wrong.** It has
#: not been checked against a pricing page or an invoice. It is non-zero on
#: purpose: zero would put image generation back on the silent-under-count path
#: this module exists to end, and a wrong-but-visible number produces a cap that
#: trips at the wrong time rather than one that never trips at all. Replace it
#: before relying on ``DAILY_COST_LIMIT`` being accurate for images.
#:
#: Keyed by family. ``gemini-3.1-flash-image`` also matches its ``-preview``
#: sibling by prefix, and does **not** match ``gemini-3.1-flash-lite-image``,
#: which is a different (cheaper) model and gets its own entry when someone
#: uses it.
IMAGE_PRICES: dict[str, ImagePrice] = {
    "gemini-3.1-flash-image": ImagePrice(Decimal("0.04")),  # UNVERIFIED
    "gemini-3-pro-image": ImagePrice(Decimal("0.14")),  # UNVERIFIED
    "imagen-4.0": ImagePrice(Decimal("0.04")),  # UNVERIFIED
    "mock-image": ImagePrice(Decimal(0)),
}

#: Models already warned about, so an unpriced model logs once per process
#: rather than once per call. A twenty-image fan-out would otherwise emit
#: twenty identical warnings and bury the one that mattered.
_warned: set[str] = set()


def _lookup[T](table: dict[str, T], model: str) -> T | None:
    """Longest-prefix match, so the more specific entry always wins.

    Iterating the table in arbitrary order and taking the first prefix hit
    would make ``claude-opus`` resolvable by a hypothetical ``claude`` entry
    depending on dict ordering — a bug that appears only when someone adds a
    shorter key.
    """
    best: T | None = None
    best_len = -1
    for prefix, price in table.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    return best


def _unpriced(model: str, kind: str) -> None:
    if model in _warned:
        return
    _warned.add(model)
    logger.warning(
        "no price entry; spend for this model will be under-counted",
        extra={"model": model, "kind": kind},
    )


def estimate_llm_cost(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float:
    """Estimated USD for one completion. Unknown model → ``0.0`` and a warning.

    Zero rather than raising: a missing price entry must not fail a generation
    that is otherwise fine. It *does* have to be loud, because silently
    under-counting is how a cap stops capping — which is the exact failure this
    module was written to end, and re-creating it one level down would be
    embarrassing.
    """
    price = _lookup(LLM_PRICES, model)
    if price is None:
        _unpriced(model, "llm")
        return 0.0
    total = (
        Decimal(input_tokens or 0) * price.input_per_mtok
        + Decimal(output_tokens or 0) * price.output_per_mtok
    ) / _PER_MILLION
    return float(total)


def estimate_image_cost(model: str, images: int | None) -> float:
    """Estimated USD for one image call. Unknown model → ``0.0`` and a warning."""
    price = _lookup(IMAGE_PRICES, model)
    if price is None:
        _unpriced(model, "image")
        return 0.0
    return float(price.per_image * Decimal(images or 0))
