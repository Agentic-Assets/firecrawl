"""Real repeated signals must not strand the owned subprocess wait lock."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

import pytest

import cre_guarded_tier as tier


SIGNAL_OWNER = r"""
import inspect,json,os,signal,subprocess,sys,threading
from cre_guarded_tier import work_deadline
from cre_owned_processes import OwnedProcessGroup,settle_owned_groups
source,start=inspect.getsourcelines(subprocess.Popen._wait)
# Target CPython's vulnerable timed-wait boundary without replacing its lock or
# wait implementation: acquire has succeeded, try/finally has not begun.
target=start+next(i for i,line in enumerate(source) if line.strip()=='try:')
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
    start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
second=[]
def trace(frame,event,arg):
    if (event=='line' and frame.f_code is subprocess.Popen._wait.__code__
        and frame.f_lineno==target and frame.f_locals.get('timeout') is not None
        and p._waitpid_lock.locked() and not second):
        second.append(True)
        os.kill(os.getpid(),getattr(signal,sys.argv[2]))
    return trace
report={}
timer=threading.Timer(.05,lambda:os.kill(os.getpid(),getattr(signal,sys.argv[1])))
try:
    with work_deadline(2):
        sys.settrace(trace)
        timer.start()
        try:
            p.wait()
        except KeyboardInterrupt as exc:
            sys.settrace(None)
            report['interruption']=type(exc).__name__
            report['lock_stranded']=p._waitpid_lock.locked()
            result=settle_owned_groups([OwnedProcessGroup.from_spawn(p)],
                grace_seconds=.02,kill_seconds=.1)
            report['settled']=result.settled
            report['groups']=result.groups
            report['errors']=result.errors
    report['second_signal_delivered']=bool(second)
finally:
    sys.settrace(None)
    timer.join(1)
    # Failure cleanup belongs only to this synthetic child's actual parent.
    # Product code must never unlock Popen internals to claim settlement.
    if p._waitpid_lock.locked(): p._waitpid_lock.release()
    try: os.killpg(p.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    p.wait(timeout=2)
    try: os.killpg(p.pid,0); report['harness_group_absent']=False
    except ProcessLookupError: report['harness_group_absent']=True
print(json.dumps(report))
"""


@pytest.mark.skipif(sys.platform != "linux", reason="Linux signal ownership")
@pytest.mark.parametrize(
    "first,second",
    [
        ("SIGINT", "SIGINT"),
        ("SIGINT", "SIGTERM"),
        ("SIGTERM", "SIGINT"),
        ("SIGALRM", "SIGINT"),
        ("SIGALRM", "SIGTERM"),
        ("SIGINT", "SIGALRM"),
    ],
)
def test_first_interruption_coalesces_second_real_signal(first, second):
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(SIGNAL_OWNER), first, second],
        cwd=Path(tier.__file__).parent,
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "PYTHONPATH": str(Path(tier.__file__).parent)},
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    report = json.loads(completed.stdout)
    assert report["harness_group_absent"] is True, report
    assert report["second_signal_delivered"] is True, report
    assert report["lock_stranded"] is False, report
    assert report["settled"] is True, report
    assert report["errors"] == [], report
    assert all(g["leader_reaped"] and g["group_gone"] for g in report["groups"])
    assert report["interruption"] == (
        "TierDeadline" if first == "SIGALRM" else "KeyboardInterrupt"
    ), report


def test_repeated_stop_restores_callers_handlers_and_disarms_timer():
    previous = {
        s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    }
    try:
        with pytest.raises(KeyboardInterrupt):
            with tier.work_deadline(2):
                os.kill(os.getpid(), signal.SIGINT)
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        assert {s: signal.getsignal(s) for s in previous} == previous
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
