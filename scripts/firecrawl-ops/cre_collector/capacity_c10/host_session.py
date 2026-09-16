"""Compatibility facade for the cohesive C10 host modules.

Public API remains intentionally small; ownership is split by responsibility.
"""

from .host_crypto import _OpenSsl
from .host_registry import C10SealedCardRegistry
from .host_sidecar import C10EphemeralKeys, DockerComposeSidecar, SidecarLifecycle
from .host_store import C10SessionStore, PrivateReceiptStore

__all__ = [
    "C10EphemeralKeys",
    "C10SealedCardRegistry",
    "C10SessionStore",
    "DockerComposeSidecar",
    "PrivateReceiptStore",
    "SidecarLifecycle",
    "_OpenSsl",
]
