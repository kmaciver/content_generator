"""Per-series auto-approval (SADD §11).

The challenge this answers: the brief mandates a human gate at every stage,
which is six approvals per video. At one video that is diligence; at fifty a
week it is the bottleneck, and the predictable outcome is a human clicking
Approve without looking — a gate that is worse than no gate because it
launders unreviewed output as reviewed.

So gates are configurable, and **the default is all-manual** — the decision
recorded in M0. Turning a gate off is a deliberate per-series act that the
audit trail records, not a default someone inherits by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from videoforge_shared.enums import ArtifactKind

__all__ = ["ApprovalPolicy", "AUTO_APPROVE_KEY"]

#: Key inside ``series.auto_approve_policy`` holding the per-kind flags.
AUTO_APPROVE_KEY = "auto_approve"


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Which artifact kinds may skip the human gate for one series."""

    auto_approve: frozenset[ArtifactKind] = field(default_factory=frozenset)

    @classmethod
    def all_manual(cls) -> ApprovalPolicy:
        """The default. Every stage waits for a human."""
        return cls()

    @classmethod
    def from_jsonb(cls, raw: dict[str, Any] | None) -> ApprovalPolicy:
        """Parse ``series.auto_approve_policy``, **failing safe**.

        Anything unrecognised — a null column, a malformed blob, a kind that
        no longer exists after a rename — yields a *stricter* policy, never a
        looser one. A corrupt config that silently auto-approved a whole
        pipeline would publish unreviewed video, which is the one outcome
        this system exists to prevent; a corrupt config that asks for an
        unnecessary click is merely annoying.
        """
        if not raw:
            return cls.all_manual()

        values = raw.get(AUTO_APPROVE_KEY)
        if not isinstance(values, list):
            return cls.all_manual()

        kinds: set[ArtifactKind] = set()
        for value in values:
            try:
                kinds.add(ArtifactKind(value))
            except ValueError:
                # Unknown kind: ignore this entry, keep the gate.
                continue
        return cls(auto_approve=frozenset(kinds))

    def to_jsonb(self) -> dict[str, Any]:
        """Round-trips through :meth:`from_jsonb`. Sorted for stable diffs."""
        return {AUTO_APPROVE_KEY: sorted(kind.value for kind in self.auto_approve)}

    def is_automatic(self, kind: ArtifactKind) -> bool:
        """Whether ``kind`` may be approved without a human."""
        return kind in self.auto_approve

    def requires_human(self, kind: ArtifactKind) -> bool:
        return not self.is_automatic(kind)

    def with_automatic(self, *kinds: ArtifactKind) -> ApprovalPolicy:
        """A copy with ``kinds`` added. The dataclass is frozen by design —
        a policy that mutates in place is one that can change between the
        capability check and the write."""
        return ApprovalPolicy(auto_approve=self.auto_approve | frozenset(kinds))

    def without_automatic(self, *kinds: ArtifactKind) -> ApprovalPolicy:
        return ApprovalPolicy(auto_approve=self.auto_approve - frozenset(kinds))
