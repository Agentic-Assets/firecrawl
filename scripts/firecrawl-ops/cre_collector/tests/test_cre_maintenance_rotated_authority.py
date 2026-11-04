"""Release witnesses must match a real SharedLock rotation."""

import copy

import pytest

import cre_checkpoint_refresh as checkpoint
import cre_code_handoff as handoff
from cre_code_handoff_evidence import HandoffError
from cre_runtime_maintenance_transition import validate_released_authority


@pytest.fixture
def rotation(tmp_path):
    with checkpoint.SharedLock(tmp_path / "fixture.lock", recovery_required=True) as lease:
        fields = checkpoint.SharedLock._authority_fields(lease.authority_fd)
        owned = {"pid": fields[0], "token": fields[1], "generation": fields[2],
                 "authority_identity": list(lease.authority_identity)}
        lease.clear_recovery_requirement()
        fields, inode = handoff.released_authority(checkpoint, lease)
        released = [list(fields), list(inode)]
        yield owned, released


def test_actual_lock_rotation_is_accepted(rotation):
    owned, released = rotation
    assert released[0][2] != owned["generation"]
    validate_released_authority(owned, released)


@pytest.mark.parametrize("change", ["pid", "token", "inode", "generation", "old", "normal", "shape", "type"])
def test_rotated_authority_rejects_forgery(rotation, change):
    owned, released = rotation
    released = copy.deepcopy(released)
    if change == "pid":
        released[0][0] += 1
    elif change == "token":
        released[0][1] += "x"
    elif change == "inode":
        released[1][1] += 1
    elif change == "generation":
        released[0][2] = "!" * 43
    elif change == "old":
        released[0][2] = owned["generation"]
    elif change == "normal":
        released[0][3] = 0
    elif change == "shape":
        released[0].append("extra")
    else:
        released[1] = tuple(released[1])
    with pytest.raises(HandoffError):
        validate_released_authority(owned, released)
