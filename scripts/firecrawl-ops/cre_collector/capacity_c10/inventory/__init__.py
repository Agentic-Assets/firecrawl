"""Offline candidate verifiers for C10 authoritative-inventory sources.

These adapters deliberately remain outside ``capacity_c10.adapters.default_registry``.
They validate sealed evidence only and never perform transport, cache, database,
status, scheduler, model, or OCR work.
"""

from .buildout import BullRealtyAdapter, LeeAssociatesAdapter, SvnAdapter
from .cbre import CbreAdapter
from .cbre_dealflow import CbreDealflowAdapter
from .cushman_wakefield import CushmanWakefieldAdapter
from .newmark import NewmarkAdapter
from .srs import SrsAdapter

__all__ = [
    "BullRealtyAdapter",
    "CbreAdapter",
    "CbreDealflowAdapter",
    "CushmanWakefieldAdapter",
    "LeeAssociatesAdapter",
    "NewmarkAdapter",
    "SrsAdapter",
    "SvnAdapter",
]
