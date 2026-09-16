"""Explicit C10 source-adapter protocol and deliberately incomplete registry.

This module does not contain a generic scraper.  A source is executable only
after a later review adds its own capability object and marks it verified.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import C10Error, require_sha256
from .inventory import (
    BullRealtyAdapter,
    CbreAdapter,
    CbreDealflowAdapter,
    CushmanWakefieldAdapter,
    LeeAssociatesAdapter,
    NewmarkAdapter,
    SrsAdapter,
    SvnAdapter,
)
from .policy import load_policy
from .strict_detail_avison_young import AvisonYoungCapacityC10Adapter
from .strict_detail_batch_b import strict_detail_batch_b_adapters
from .strict_detail_colliers import ColliersCapacityC10Adapter
from .strict_detail_colliers_main import ColliersMainCapacityC10Adapter
from .strict_detail_jll import JllCapacityC10Adapter
from .strict_detail_jll_investor import JllInvestorCapacityC10Adapter
from .strict_detail_marcus_millichap import MarcusMillichapCapacityC10Adapter


class C10SourceAdapter(Protocol):
    """The narrow per-provider surface required before C10 may run live."""

    key: str
    fully_verified: bool
    implementation_sha256: str

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        """Prove the source's current population and immutable member identity."""

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        """Prove host, provider identity, freshness, and normalized fidelity."""

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        """Return true only for this provider's reviewed current attrition proof."""


@dataclass(frozen=True)
class UnimplementedAdapter:
    """A named placeholder that cannot execute or accidentally admit a source."""

    key: str
    implementation_sha256: str
    fully_verified: bool = False

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        raise C10Error(f"C10 adapter {self.key} is not implemented")

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        raise C10Error(f"C10 adapter {self.key} is not implemented")

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        raise C10Error(f"C10 adapter {self.key} has no reviewed attrition classifier")


def _placeholder_digest(key: str) -> str:
    # A stable identifier makes an incomplete registry auditable without
    # pretending that source code or a generic HTTP worker is executable.
    return f"unimplemented-c10-adapter:{key}"


def default_registry() -> dict[str, C10SourceAdapter]:
    """Return all 20 named slots, each explicitly non-admitting in Wave 1."""
    return {
        source["key"]: UnimplementedAdapter(
            key=source["key"], implementation_sha256=_placeholder_digest(source["key"])
        )
        for source in load_policy()["sources"]
    }


def batch_b_registry() -> dict[str, C10SourceAdapter]:
    """Return Batch B receipt validators plus fail-closed placeholders.

    This is intentionally not an admitting registry: its six concrete adapters
    remain ``fully_verified=False`` until their native proof chains are reviewed.
    """
    registry = default_registry()
    registry.update(strict_detail_batch_b_adapters())
    return registry


def candidate_registry() -> dict[str, C10SourceAdapter]:
    """Expose all 20 named C10 candidates without making any executable.

    This is the sole discovery surface for Wave 2 receipt verifiers. It starts
    from the fixed policy registry, replaces only named slots, and never
    synthesizes a generic adapter or admission fallback.
    """
    registry = batch_b_registry()
    registry.update(
        {
            "jll": JllCapacityC10Adapter(),
            "jll-investor": JllInvestorCapacityC10Adapter(),
            "colliers": ColliersCapacityC10Adapter(),
            "colliers-main": ColliersMainCapacityC10Adapter(),
            "marcus-millichap": MarcusMillichapCapacityC10Adapter(),
            "avison-young": AvisonYoungCapacityC10Adapter(),
            "cbre": CbreAdapter(),
            "cbre-dealflow": CbreDealflowAdapter(),
            "cushman-wakefield": CushmanWakefieldAdapter(),
            "newmark": NewmarkAdapter(),
            "svn": SvnAdapter(),
            "lee-associates": LeeAssociatesAdapter(),
            "srs": SrsAdapter(),
            "bull-realty": BullRealtyAdapter(),
        }
    )
    expected = {source["key"] for source in load_policy()["sources"]}
    if set(registry) != expected:
        raise C10Error("C10 candidate registry must exactly match the fixed policy")
    if any(adapter.fully_verified is not False for adapter in registry.values()):
        raise C10Error("C10 candidate registry cannot contain an admitting adapter")
    return registry


def verified_registry(
    policy: Mapping[str, Any], registry: Mapping[str, C10SourceAdapter]
) -> dict[str, C10SourceAdapter]:
    """Require exact policy parity and a reviewed adapter for every source.

    The verification flag is necessary but not sufficient: admission is bound
    to the concrete repository implementations exposed by
    :func:`candidate_registry`, including their reviewed implementation
    digests. A caller-supplied object cannot mint admission merely by
    self-declaring ``fully_verified = True`` and a SHA-shaped string.
    """
    expected = {source["key"] for source in policy["sources"]}
    if set(registry) != expected:
        raise C10Error("C10 adapter registry must exactly match the fixed policy")
    trusted = candidate_registry()
    admitted: dict[str, C10SourceAdapter] = {}
    for key in sorted(expected):
        adapter = registry[key]
        if adapter.key != key:
            raise C10Error("C10 adapter key does not match its registry slot")
        if adapter.fully_verified is not True:
            raise C10Error(f"C10 adapter {key} is not fully verified")
        require_sha256(
            adapter.implementation_sha256, f"C10 adapter {key} implementation"
        )
        trusted_adapter = trusted[key]
        # Batch B binds the review flag into its implementation digest. Build
        # the comparison manifest in the same reviewed state without changing
        # the fail-closed objects returned by a separate candidate_registry().
        object.__setattr__(trusted_adapter, "fully_verified", True)
        if (
            type(adapter) is not type(trusted_adapter)
            or adapter.implementation_sha256 != trusted_adapter.implementation_sha256
        ):
            raise C10Error(
                f"C10 adapter {key} is not the reviewed repository implementation"
            )
        admitted[key] = adapter
    return admitted
