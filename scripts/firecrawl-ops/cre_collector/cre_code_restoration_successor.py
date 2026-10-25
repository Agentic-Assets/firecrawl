"""Read-only recognition of one currently active original scheduled checkpoint.

No scheduler calls, lock acquisition, stop/recovery, source writes, or subprocesses.
Caller must pass the actual native producer record. Every race refuses recognition.
"""
import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from cre_code_handoff_advancement import NS, instance
from cre_code_handoff_evidence import HandoffError, TASKS, TASK_PATH, clock, json_bytes, require, sha, unique

PROC = Path('/proc')
SOURCES = {'interra': 'interra-realty', 'newmark': 'newmark'}
COMMON = set('out-root sources page-cap concurrency source-workers attempts-per-source max-resume-age-hours max-host-cpu-percent cpu-sustain-seconds cpu-sample-seconds memory-sustain-seconds memory-sample-seconds env-file admit-current-inventory-source daum-subset-policy'.split())
PARENT = COMMON | set('work-deadline-seconds expected-sha windows-admission-unix-ms nice max-resource-recoveries-per-source recovery-low-cpu-percent recovery-low-cpu-seconds recovery-cpu-sample-seconds max-recovery-cooldown-seconds max-series-recovery-seconds'.split())
CHILD = COMMON | {'transactions', 'run-id', 'series-control-invocation-id', 'series-control-child-id'}


def _bounded(path, limit=16384):
    """Proc leaves only. Proc fd/ns links are handled explicitly elsewhere."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        data = os.read(fd, limit + 1)
        require(len(data) <= limit, 'process evidence bound exceeded')
        return data
    finally:
        os.close(fd)


def _process(pid):
    require(type(pid) is int and 1 < pid < 2147483648, 'invalid Linux owner PID')
    raw = _bounded(PROC / str(pid) / 'stat').decode('ascii')
    prefix, separator, tail = raw.rpartition(')')
    fields = tail.split()
    require(separator and prefix.startswith(str(pid) + ' (') and len(fields) >= 20
            and fields[0] in {'R', 'S', 'D', 'T', 't', 'K', 'W', 'P', 'I'}, 'Linux process unavailable')
    uid_lines = [line.split()[1:] for line in _bounded(PROC / str(pid) / 'status').decode('ascii').splitlines()
                 if line.startswith('Uid:')]
    require(len(uid_lines) == 1 and [int(x) for x in uid_lines[0]] == [os.geteuid()] * 4,
            'Linux owner UID differs')
    namespace = os.stat(PROC / str(pid) / 'ns/pid')
    boot = _bounded(PROC / 'sys/kernel/random/boot_id', 128).decode('ascii').strip()
    require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot)
            and int(fields[19]) > 0, 'Linux birth identity unavailable')
    return {'pid': pid, 'ppid': int(fields[1]), 'start_ticks': int(fields[19]), 'boot_id': boot,
            'uid': os.geteuid(), 'pid_namespace_identity': [namespace.st_dev, namespace.st_ino]}


def _argv(pid, script, allowed):
    raw = _bounded(PROC / str(pid) / 'cmdline', 32768)
    require(raw.endswith(b'\0'), 'incomplete process scalar input')
    argv = raw[:-1].decode('utf-8').split('\0')
    require(len(argv) >= 2 and Path(argv[0]).name in {'python', 'python3', 'python3.12', 'python3.13'}
            and argv[1] == str(script), 'foreign checkpoint interpreter/script')
    flags = {}
    # Only the original serial/pipeline fresh-run routes are supported.
    index = 2
    while index < len(argv):
        flag = argv[index]
        if flag in {'--admit-baseline-hold-additively', '--pipeline'}:
            require(flag[2:] not in flags, 'duplicate checkpoint flag')
            flags[flag[2:]] = True
            index += 1
            continue
        require(index + 1 < len(argv), 'unsupported checkpoint flag shape')
        flag, value = argv[index:index + 2]
        require(flag.startswith('--') and flag[2:] in allowed
                and (flag[2:] not in flags or flag == '--admit-current-inventory-source')
                and value and not value.startswith('--'), 'unknown/duplicate checkpoint scalar')
        if flag == '--admit-current-inventory-source':
            values = flags.setdefault(flag[2:], [])
            require(value not in values and len(values) < 64, 'duplicate/excess source admission')
            values.append(value)
        else:
            flags[flag[2:]] = value
        index += 2
    return flags


def _private_read(path, read_file, limit):
    raw = read_file(path)
    require(isinstance(raw, bytes) and len(raw) <= limit, 'private evidence exceeds bound')
    return raw


def _authority(checkpoint, lock):
    """Use existing safe read-only authority reader, never a SharedLock context."""
    import cre_stop_guarded_tier as safe
    result = safe._authority(lock)
    pid = result['pid']
    # Find an exact held-fd witness; no attempt to acquire somebody else's flock.
    entries = list((PROC / str(pid) / 'fdinfo').iterdir())
    require(len(entries) <= 256, 'owner descriptor bound exceeded')
    witnesses = []
    for path in entries:
        require(path.name.isdecimal(), 'malformed owner descriptor')
        descriptor = int(path.name)
        try:
            observed = os.stat(PROC / str(pid) / 'fd' / path.name)
        except FileNotFoundError:
            continue
        if [observed.st_dev, observed.st_ino] != result['authority_identity']:
            continue
        owner = dict(result, authority_fd=descriptor)
        safe._held_flock(owner)
        witnesses.append(descriptor)
    require(len(witnesses) == 1, 'canonical authority held-fd ambiguous')
    return dict(result, authority_fd=witnesses[0])


def _task_sources(packet, key):
    row = unique(packet['task_baseline'], 'key', TASKS)[key]
    arguments = row['actions'][0]['arguments']
    values = re.findall(r'(?:^|\s)"?-Sources?"?\s+(?:"([a-z0-9,-]+)"|([a-z0-9,-]+))(?=\s|$)', arguments)
    require(len(values) == 1, 'exact installed task source scalar missing')
    return values[0][0] or values[0][1]


def _expected_config(source, config):
    from cre_checkpoint_series import _expected_child_config
    return _expected_child_config(source, config)


def _native(packet, record, bridge, source, code, stamp, evidence):
    key = next((key for key, name in TASKS.items() if bridge.get('task_path') == TASK_PATH + name), None)
    require(key is not None and source == _task_sources(packet, key), 'unsupported scheduled source')
    task = unique(record.get('tasks'), 'key', TASKS)[key]
    principal = unique(record.get('principals'), 'key', TASKS)[key]
    baseline = unique(packet['task_baseline'], 'key', TASKS)[key]
    baseline_xml = ET.fromstring(evidence.raw(baseline['xml']))
    require(principal.get('user_id') == baseline_xml.findtext('{*}Principals/{*}Principal/{*}UserId'),
            'native principal not bound to installed baseline')
    guid = instance(bridge.get('task_instance_guid'))
    matching = [row for row in record.get('instances', []) if row.get('key') == key
                and instance(row.get('instance_guid')) == guid]
    require(len(matching) == 1 and type(matching[0].get('engine_pid')) is int and matching[0]['engine_pid'] > 0
            and task.get('state') == 'Running', 'scheduled native instance not active')
    for name in ('action_pid', 'direct_wsl_pid', 'direct_wsl_parent_pid'):
        require(type(bridge.get(name)) is int and bridge[name] > 0, 'native process bridge malformed')
    require(bridge['action_pid'] == bridge['direct_wsl_parent_pid'] != bridge['direct_wsl_pid']
            and bridge.get('scalar_argv') == {'expected-sha': code, 'sources': source,
                                             'windows-admission-unix-ms': str(stamp)}, 'native admission scalars differ')
    lower, end = record['watermark'], clock(record['finished_at'])
    events = {}
    require(len(record.get('events', [])) <= 768, 'native event bound exceeded')
    for raw in record['events']:
        require(isinstance(raw, str) and len(raw) <= 65536 and '<!DOCTYPE' not in raw.upper()
                and '<!ENTITY' not in raw.upper(), 'native XML bound/declaration refused')
        root = ET.fromstring(raw)
        system = root.find('e:System', NS)
        require(system is not None and system.find('e:Provider', NS).get('Name') == 'Microsoft-Windows-TaskScheduler'
                and system.findtext('e:Channel', namespaces=NS) == 'Microsoft-Windows-TaskScheduler/Operational'
                and system.findtext('e:Computer', namespaces=NS) == record.get('computer'), 'native event producer differs')
        data = {}
        for node in root.findall('e:EventData/e:Data', NS):
            require(node.get('Name') not in data, 'duplicate native event key')
            data[node.get('Name')] = node.text
        if data.get('TaskName') != bridge['task_path']:
            continue
        event_id = int(system.findtext('e:EventID', namespaces=NS))
        if event_id not in {107, 129, 100, 200, 201, 102}:
            continue
        # 129 does not universally carry a GUID; join its exact action PID instead.
        if event_id == 129:
            if data.get('ProcessID') != str(bridge['action_pid']):
                continue
        elif instance(data.get('InstanceId') or data.get('TaskInstanceId')) != guid:
            continue
        require(event_id not in events and event_id not in {201, 102}, 'duplicate/terminal active instance')
        rid = int(system.findtext('e:EventRecordID', namespaces=NS))
        when = clock(system.find('e:TimeCreated', NS).get('SystemTime'))
        require(lower['record_id'] < rid <= record['upper_record_id']
                and clock(lower['captured_at']) <= when <= end, 'native event outside watermark')
        events[event_id] = (rid, when, data)
    require(set(events) == {107, 129, 100, 200}, 'native scheduled start bridge incomplete')
    ordered = [events[event] for event in (107, 129, 100, 200)]
    require(bridge.get('scheduler_event_record_ids') == [row[0] for row in ordered]
            and all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(ordered, ordered[1:])),
            'native event bridge chronology differs')
    require(events[100][2].get('UserContext', '').casefold() in
            {alias.casefold() for alias in principal.get('aliases', [principal['user_id']])}
            and events[200][2].get('ActionName') == task['actions'][0]['execute']
            and events[200][2].get('EnginePID') == str(bridge['action_pid']), 'native action/principal differs')
    admitted = datetime.fromtimestamp(stamp / 1000, UTC)
    require(events[107][1] <= clock(bridge['action_creation_utc']) <= events[129][1]
            <= events[200][1] <= admitted <= clock(bridge['direct_wsl_creation_utc'])
            <= clock(bridge['observed_at']) <= end, 'native process bridge clocks differ')
    return key


def observe_scheduled_successor(packet, checkpoint, lock, released, native_record, evidence, *, read_file):
    """Recognize an active original scheduled successor; raise on all uncertainty.

    This finite reader requires freshly produced bridges. It does not certify
    completed busy exits, provider/database work, or source settlement.
    """
    try:
        import cre_series_control as control
        import cre_stop_guarded_tier as safe
        lock = Path(lock)
        require(released is not None and len(released) == 2 and released[0][3] is False,
                'actual released authority witness missing')
        before = _authority(checkpoint, lock)
        require(tuple(before['authority_identity']) == tuple(released[1])
                and before['authority_token'] != released[0][1]
                and before['authority_generation'] != released[0][2]
                and before['pid'] != released[0][0], 'released predecessor/current successor differs')
        child = _process(before['pid'])
        collector = Path(packet['roots']['runtime']) / 'scripts/firecrawl-ops/cre_collector'
        args = _argv(child['pid'], collector / 'cre_checkpoint_refresh.py', CHILD)
        invocation, child_id = args.get('series-control-invocation-id'), args.get('series-control-child-id')
        require(all(isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) for value in (invocation, child_id)),
                'active child invocation identity missing')
        run_id = args.get('run-id')
        require(isinstance(run_id, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{6}Z-[0-9a-f]{12}', run_id),
                'fresh checkpoint run identity missing')
        series = Path(args['out-root']).parent
        require(Path(args['out-root']) == series / 'runs'
                and series.parent == collector / 'out/checkpoint-series'
                and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{6}Z', series.name), 'checkpoint namespace differs')
        parent = _process(child['ppid'])
        pargs = _argv(parent['pid'], collector / 'cre_checkpoint_series.py', PARENT)
        source, code = args.get('sources'), packet['code']['sha']
        selected_sources = source.split(',') if isinstance(source, str) else []
        require(selected_sources and len(selected_sources) == len(set(selected_sources)) <= 64
                and all(re.fullmatch('[a-z0-9-]+', member) for member in selected_sources)
                and pargs.get('sources') == source
                and pargs.get('expected-sha') == code and args.get('transactions') == 'both',
                'active parent/child source or code differs')
        stamp = int(pargs['windows-admission-unix-ms'])
        require(stamp > 0, 'active parent admission clock missing')
        manifest_path = series / 'manifest.json'
        run_path = series / 'runs' / run_id / 'manifest.json'
        run_raw = _private_read(run_path, read_file, 1024 * 1024)
        run = json_bytes(run_raw)
        require(run.get('run_id') == run_id and run.get('collector_git_sha') == code
                and run.get('collector_git_dirty') is False and set(run.get('sources', {})) == set(selected_sources),
                'active child manifest code/run differs')
        raw = _private_read(manifest_path, read_file, 1024 * 1024)
        manifest = json_bytes(raw)
        receipt = control.read_receipt(series, invocation)
        owner = receipt['stop_owner']
        require(all(owner.get(key) == value for key, value in parent.items() if key != 'ppid')
                and receipt.get('status') == 'running', 'series stop owner differs')
        control.validate_live_owner(series, receipt)
        require(manifest.get('series_id') == series.name and manifest.get('collector_git_sha') == code
                and manifest.get('collector_git_dirty') is False and set(manifest.get('sources', {})) == set(selected_sources)
                and manifest.get('latest_invocation_admission') == {'expected_sha': code, 'windows_admission_unix_ms': stamp}
                and manifest.get('latest_invocation_control', {}).get('invocation_id') == invocation
                and manifest['latest_invocation_control'].get('receipt') == '.series-control/' + invocation + '.json',
                'active series admission differs')
        require(bool(args.get('pipeline')) == bool(pargs.get('pipeline')) == bool(manifest.get('config', {}).get('pipeline'))
                and run.get('config') == _expected_config(source, manifest.get('config', {})),
                'active checkpoint configuration differs')
        config = manifest.get('config', {})
        for flag, field in (('source-workers', 'source_workers'), ('concurrency', 'concurrency'), ('page-cap', 'page_cap')):
            if field in config:
                require(flag in args and int(args[flag]) == config[field]
                        and (flag not in pargs or int(pargs[flag]) == config[field]),
                        'checkpoint concurrency/page configuration differs')
        for member in selected_sources:
            selected = manifest['sources'][member]
            require(selected.get('state') == 'running' and selected.get('checkpoint_run') == 'runs/' + run_id
                    and selected.get('attempts') and selected['attempts'][-1].get('series_control_child_id') == child_id
                    and selected['attempts'][-1].get('rc') is None, 'active child/run differs')
        bridges = [bridge for bridge in native_record.get('bridges', [])
                   if bridge.get('scalar_argv') == {'expected-sha': code, 'sources': source,
                                                  'windows-admission-unix-ms': str(stamp)}]
        require(len(bridges) == 1, 'missing/ambiguous authentic native scalar bridge')
        key = _native(packet, native_record, bridges[0], source, code, stamp, evidence)
        require(source == _task_sources(packet, key), 'task source selection differs')
        require((key == 'qualified') == bool(args.get('pipeline')), 'task pipeline mode differs')
        require(_process(child['pid']) == child and _process(parent['pid']) == parent
                and _argv(child['pid'], collector / 'cre_checkpoint_refresh.py', CHILD) == args
                and _argv(parent['pid'], collector / 'cre_checkpoint_series.py', PARENT) == pargs
                and _authority(checkpoint, lock) == before
                and control.read_receipt(series, invocation) == receipt
                and _private_read(run_path, read_file, 1024 * 1024) == run_raw
                and _private_read(manifest_path, read_file, 1024 * 1024) == raw,
                'scheduled successor changed during observation')
        control.validate_live_owner(series, receipt)
        evidence.original_payloads[str(manifest_path)] = raw
        evidence.original_payloads[str(run_path)] = run_raw
        return {'phase': 'released-scheduled-successor', 'verified': True, 'recovery_required': True,
                'source_settlement': 'unverified',
                'key': key, 'source': source, 'series_id': series.name, 'run_id': run_id,
                'invocation_id': invocation, 'child_invocation_id': child_id,
                'child_birth': child, 'parent_birth': parent,
                'authority_identity': before['authority_identity'],
                'lock_directory_identity': before['lock_directory_identity'],
                'authority_token_sha256': sha(before['authority_token'].encode()),
                'authority_generation_sha256': sha(before['authority_generation'].encode()),
                'scheduler_event_record_ids': bridges[0]['scheduler_event_record_ids'],
                'task_instance_guid': bridges[0]['task_instance_guid'],
                'run_reference': {'path': str(run_path), 'bytes': len(run_raw), 'sha256': sha(run_raw)},
                'manifest_reference': {'path': str(manifest_path), 'bytes': len(raw), 'sha256': sha(raw)}}
    except HandoffError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, ET.ParseError, control.SeriesControlError, safe.StopRefused) as error:
        raise HandoffError('scheduled successor ownership pending/unverified') from error
