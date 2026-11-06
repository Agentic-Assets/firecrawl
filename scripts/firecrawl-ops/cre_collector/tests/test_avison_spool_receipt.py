import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cre_partial_page_custody import prepare
from source_evidence.avison_spool_receipt import verify_receipt


COLLECTOR = Path(__file__).resolve().parents[1]
WORKER = """
import {PartialPageSpool} from './lib/partial-page-spool.ts';
import {publishAvisonSpoolReceipt} from './lib/source-evidence/avison-spool-receipt.ts';
const {SPOOL, GENERATION, STARTED, CRASH_POINT} = process.env;
const spool = new PartialPageSpool(SPOOL, GENERATION, 1, undefined, 'avison-young');
const observed = new Date().toISOString();
spool.record({transaction:'sale',page:1,pass:1,kind:'validated_detail',observedAt:observed,
  documents:[{id:'1',sourceKey:'avison-young'}]});
spool.record({transaction:'sale',page:1,pass:1,kind:'detail_outcomes',observedAt:observed,
  documents:[{id:'1',state:'validated'},{id:'2',state:'failed'}]});
const receipt = spool.finish();
if(CRASH_POINT==='before')process.kill(process.pid,'SIGKILL');
publishAvisonSpoolReceipt(SPOOL,GENERATION,1,receipt,STARTED,new Date().toISOString(),'record_local_error');
process.kill(process.pid,'SIGKILL');
"""


@pytest.mark.parametrize("point", ["before", "after"])
def test_process_death_before_or_after_receipt_preserves_only_bound_prefix(tmp_path, point):
    node = shutil.which("node")
    if node is None or not (COLLECTOR / "node_modules/tsx").exists():
        pytest.skip("requires existing Node/tsx")
    root = tmp_path / "generation"
    root.mkdir(mode=0o700)
    state = prepare(root, 1, "avison-young")
    started = datetime.now(UTC).isoformat()
    env = dict(os.environ, SPOOL=str(root / state["path"]), GENERATION=root.name,
               STARTED=started, CRASH_POINT=point)
    child = subprocess.run([node, "--import", "tsx", "--input-type=module", "-e", WORKER],
                           cwd=COLLECTOR, env=env, capture_output=True, text=True, timeout=10, check=False)
    assert child.returncode == -9
    attempt = {"number": 1, "rc": child.returncode, "started_at": started,
               "finished_at": datetime.now(UTC).isoformat(), "avison-young_partial": state}
    result = verify_receipt(root, attempt)
    if point == "before":
        assert result is None
        return
    assert result and result["state"] == "sealed_standalone"
    assert result["coverage"] == "unverified"
    # A crash after link and before unlink leaves two names for one inode.
    # This state is refused, then accepted only after the extra link is gone.
    receipt = root / result["receipt_path"]
    extra = receipt.parent / ".receipt-synthetic.tmp"
    os.link(receipt, extra)
    assert verify_receipt(root, attempt) is None
    extra.unlink()
    assert verify_receipt(root, attempt) is not None
    rows = [json.loads(line) for line in (root / state["path"]).read_text().splitlines()[1:]]
    assert rows[0]["documents"][0]["id"] == "1"
    assert rows[1]["documents"][1]["state"] == "failed"
    attempt["rc"] = 73
    assert verify_receipt(root, attempt) is None
    attempt["rc"] = child.returncode
    with (root / state["path"]).open("ab") as output:
        output.write(b"tampered\n")
    assert verify_receipt(root, attempt) is None
