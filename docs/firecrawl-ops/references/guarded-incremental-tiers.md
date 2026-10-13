Governed by [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752), implementation [AGENTIC-4141](https://linear.app/agenticassets/issue/AGENTIC-4141).

# Guarded incremental CRE trials

The Windows desktop uses Docker Desktop with the existing WSL2 backend and an
ext4 Ubuntu checkout. `cre_guarded_tier.py` adds a foreground trial entrypoint
for explicit-source inventory observation and small enrichment batches. It
does not install or enable a scheduler. Existing Mac launchd commands retain
their behavior; they do not acquire these new protections automatically.

## What each mode does

| Invocation | Provider work | Database work |
| --- | --- | --- |
| `monitor --source KEY` | Current inventory for that source, collector concurrency1 | None; monitor transforms the artifact in dry-run mode |
| `enrich --source KEY` | None | None; prints the proposed queue claim SQL |
| `enrich --source KEY --apply` | Details for an exact-source queue batch | Claims, additive listing ingest, queue completion/retry accounting |

Monitor `--apply` is refused. JLL, JLL Investor, CBRE Deal Flow and Colliers
SalesTracker are excluded from inventory monitoring; use separately admitted
checkpoint runs. Inventory observation does not refresh listing detail or
enqueue new work. The enrichment SQL dry-run does not test rendering.

An applied enrichment uses the existing worker semantics, with an additional
database target fingerprint checked before claims and passed to the ingest
child. An uncertain bound-ingest failure retains claims for reconciliation.
No mode enables status activation, mark-missing, baseline updates, OM extraction
or DDL. No-write inventory success does not admit a source through the strict
coverage gate or certify a full refresh.

## Run a supervised trial

First read `cre_status.sh --scheduler external`, current sole-writer evidence,
the exact pushed commit and the source's current admission contract. Refresh
remote tracking refs explicitly before a run. The entrypoint requires a clean
checkout, exact expected SHA and a matching local `origin` tracking tip; it
does not fetch, pull or prove current remote state itself.

From PowerShell, sample Windows without starting WSL or writing files:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 -CheckOnly
```

Run a bounded no-write inventory trial after deploying the reviewed commit to
the existing Ubuntu checkout:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 `
  -Tier monitor -Source interra-realty -ExpectedSha <full-reviewed-sha> `
  -DeadlineSeconds 600
```

The defaults select `Ubuntu-24.04`, Linux user `cayman`,
`/home/cayman/code/firecrawl`, and the private
`/home/cayman/.config/cre/equire.env`. Override the explicit parameters on
another host. Never pass a database URL or password on the command line.

Linux operators can invoke the same foreground entrypoint directly:

```bash
python3 cre_guarded_tier.py monitor --source interra-realty \
  --expected-sha <full-reviewed-sha> --deadline-seconds 600
python3 cre_guarded_tier.py enrich --source hanley --batch 2 \
  --expected-sha <full-reviewed-sha> --deadline-seconds 600
```

Adding `--apply` to the enrichment command performs real queue and listing
writes. Require a permitted supervised additive trial, approved target,
healthy existing services and exact artifact/database readback before doing
that. Use the real generation artifact to assess data freshness and quality;
a successful worker exit alone is not a production acceptance certificate.

## Protection and operator controls

The Linux owner arms the existing canonical recovery fence before any native
child, reuses checkpoint CPU/memory watchdogs and owns each subprocess group.
The batch is capped at50, and the work deadline is30 to14400 seconds. Cleanup
has the checkpoint's separate grace/join budgets; the work deadline does not
preempt a blocked kernel call or certify cancellation of remote work.

The PowerShell launcher measures whole-Windows CPU and physical memory using
Microsoft's native APIs. It refuses admission at90% CPU or below8GiB available
physical memory, and refuses missing, invalid, stale or unsupported-topology
evidence. These are conservative local admission settings, not Microsoft
standards. The Linux owner rejects stale/future Windows admission timestamps
before work. This is start-time host admission. Later native Windows load is
not monitored; Linux watchdogs observe the VM and visible cgroups. Retain the
current WSL ceiling and reassess from measured runs.

Linux receipts and guard journals are under `out/guarded-tiers/<run>/`.
Windows launch audit receipts are under `out/windows-launcher/` in the Windows
checkout. They are audit artifacts, not locks or scheduling authority. Receipt
paths and exit status are returned in the foreground.

| Exit | Meaning |
| --- | --- |
| `0` | Selected trial completed; inspect its scope and receipt |
| `1` | Refusal/failure; inspect receipt and canonical recovery state |
| `73` | Active owner or recovery barrier prevented dispatch |
| `75` | Required resource evidence or pressure prevented/interrupts work |
| `124` | Work deadline expired; inspect settlement and recovery state |
| `130` | Operator interruption; inspect settlement and recovery state |

For a direct Linux foreground trial, use Ctrl+C for cooperative cancellation.
Do not delete lock files or retry around an armed recovery barrier. Follow the
existing checkpoint recovery procedure and reconcile uncertain provider/DB
work before a successor. The Windows launcher's disappearance or Task
Scheduler's **End** action does not prove Linux work stopped:
[Microsoft documents that wslhost can retain its lifetime](https://github.com/microsoft/WSL/blob/master/doc/docs/technical-documentation/wslhost.exe.md).
The independent Linux deadline still applies to a surviving owner. Abrupt
Linux owner death leaves the durable fence armed.

## Scheduling remains a separate acceptance step

The proposed deterministic surface is Windows Task Scheduler calling this
foreground launcher, with `IgnoreNew`, the same signed-in user as Docker
Desktop, reviewed wake/catchup behavior, visible history and failure controls.
No continuously running ChatGPT monitor is needed for deterministic work.
Do not schedule the legacy monitor/enrich/weekly shell commands as a shortcut.
Weekly completeness stays on the guarded checkpoint-series procedure.

Exact surface/cadence, registry approval, actual scheduled cancellation/login
tests and supervised additive readback remain prerequisites on AGENTIC-3752
and AGENTIC-4141. The Codex weekly proposal remains a separately controlled
paused alternative. Full51-source acceptance remains AGENTIC-2902; a trial
does not resolve missing baselines, historical quality findings or a source's
legitimate low inventory count.
