"""Keep evidence unit tests isolated from every live CRE lock and fence."""

import cre_checkpoint_refresh as checkpoint
import pytest


@pytest.fixture(autouse=True)
def isolate_evidence_shared_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, "canonical_shared_lock_dir", lambda: tmp_path / "shared.lock")
