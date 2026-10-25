"""Fixture-only proof of exact current scheduled-successor joins."""
import copy
import json
import types
from pathlib import Path

import pytest
import cre_code_restoration_successor as subject
from cre_code_handoff_evidence import HandoffError


@pytest.fixture
def active(monkeypatch):
    import cre_series_control as control
    root = Path('/runtime/scripts/firecrawl-ops/cre_collector')
    series = root / 'out/checkpoint-series/2026-10-25T030000Z'
    run = '2026-10-25T030000Z-' + 'a' * 12
    invocation, child_id = 'b' * 32, 'c' * 32
    code = 'd' * 40
    parent = dict(pid=120, ppid=110, start_ticks=90, boot_id='0' * 8 + '-0000-0000-0000-' + '0' * 12,
                  uid=1000, pid_namespace_identity=[4, 999])
    child = dict(parent, pid=121, ppid=120, start_ticks=91)
    authority = dict(pid=121, authority_token='new-token', authority_generation='new-generation',
                     authority_identity=[1, 2], lock_directory_identity=[1, 3], authority_fd=4,
                     lease_token='new-generation')
    owner = {key: value for key, value in parent.items() if key != 'ppid'}
    receipt = dict(status='running', stop_owner=owner)
    args = {'out-root': str(series / 'runs'), 'sources': 'newmark', 'transactions': 'both',
            'run-id': run, 'series-control-invocation-id': invocation, 'series-control-child-id': child_id}
    pargs = {'sources': 'newmark', 'expected-sha': code, 'windows-admission-unix-ms': '1000'}
    manifest = {'series_id': series.name, 'collector_git_sha': code, 'collector_git_dirty': False, 'config': {},
                'latest_invocation_admission': {'expected_sha': code, 'windows_admission_unix_ms': 1000},
                'latest_invocation_control': {'invocation_id': invocation, 'receipt': '.series-control/' + invocation + '.json'},
                'sources': {'newmark': {'state': 'running', 'checkpoint_run': 'runs/' + run,
                                       'attempts': [{'series_control_child_id': child_id, 'rc': None}]}}}
    run_manifest = {'run_id': run, 'collector_git_sha': code, 'collector_git_dirty': False, 'config': {'bound': True},
                    'sources': {'newmark': {}}}
    bridge = {'scalar_argv': {'sources': 'newmark', 'expected-sha': code, 'windows-admission-unix-ms': '1000'},
              'scheduler_event_record_ids': [1, 2, 3, 4], 'task_instance_guid': '{00000000-0000-0000-0000-000000000001}'}
    monkeypatch.setattr(subject, '_authority', lambda *unused: dict(authority))
    monkeypatch.setattr(subject, '_process', lambda pid: dict(child if pid == 121 else parent))
    monkeypatch.setattr(subject, '_argv', lambda pid, *unused: dict(args if pid == 121 else pargs))
    # Native XML contract has separate tests; this fixture isolates Linux joins.
    monkeypatch.setattr(subject, '_native', lambda *unused: 'newmark')
    monkeypatch.setattr(subject, '_expected_config', lambda *unused: {'bound': True})
    monkeypatch.setattr(control, 'read_receipt', lambda *unused: copy.deepcopy(receipt))
    monkeypatch.setattr(control, 'validate_live_owner', lambda *unused: None)
    def read(path):
        value = run_manifest if str(path).endswith(run + '/manifest.json') else manifest
        return json.dumps(value, sort_keys=True).encode()
    packet = {'roots': {'runtime': '/runtime'}, 'code': {'sha': code},
              'task_baseline': [{'key': key, 'actions': [{'arguments': '-Sources ' + value}]}
                                for key, value in [('interra', 'interra-realty'), ('newmark', 'newmark'),
                                                   ('qualified', 'one,two')]]}
    evidence = types.SimpleNamespace(original_payloads={})
    def call():
        return subject.observe_scheduled_successor(packet, object(), Path('/canonical/.cre.lock'),
                    ((99, 'old-token', 'old-generation', False), (1, 2)), {'bridges': [bridge]}, evidence, read_file=read)
    return types.SimpleNamespace(**locals())


def test_active_child_join_does_not_require_terminal_last_checkpoint(active):
    result = active.call()
    assert result['phase'] == 'released-scheduled-successor'
    assert result['source_settlement'] == 'unverified'
    assert result['child_birth']['start_ticks'] == 91
    assert 'authority_token' not in result
    assert len(active.evidence.original_payloads) == 2


@pytest.mark.parametrize('mutation', ['birth', 'foreign-parent', 'missing-bridge', 'wrong-child', 'wrong-code', 'same-generation'])
def test_missing_or_foreign_link_refuses(active, mutation):
    if mutation == 'birth':
        active.receipt['stop_owner']['start_ticks'] += 1
    elif mutation == 'foreign-parent':
        active.pargs['sources'] = 'interra-realty'
    elif mutation == 'missing-bridge':
        active.bridge['scalar_argv']['windows-admission-unix-ms'] = '1001'
    elif mutation == 'wrong-child':
        active.manifest['sources']['newmark']['attempts'][0]['series_control_child_id'] = 'e' * 32
    elif mutation == 'wrong-code':
        active.run_manifest['collector_git_sha'] = 'e' * 40
    else:
        active.authority['authority_generation'] = 'old-generation'
    with pytest.raises(HandoffError):
        active.call()


def test_racing_authority_refuses(active, monkeypatch):
    count = 0
    def changed(*unused):
        nonlocal count
        count += 1
        value = dict(active.authority)
        if count > 1:
            value['authority_token'] = 'raced'
        return value
    monkeypatch.setattr(subject, '_authority', changed)
    with pytest.raises(HandoffError, match='changed during observation'):
        active.call()


@pytest.mark.parametrize('argv', [
    b'/usr/bin/python3\0/runtime/script.py\0--sources\0newmark\0--sources\0newmark\0',
    b'/usr/bin/python3\0/runtime/script.py\0--sources\0newmark\0--apply\0',
    b'/usr/bin/python3\0/runtime/script.py\0--sources\0newmark',
    b'/usr/bin/python3\0/foreign/script.py\0--sources\0newmark\0',
])
def test_cmdline_duplicate_unknown_incomplete_foreign_refused(monkeypatch, argv):
    monkeypatch.setattr(subject, '_bounded', lambda *unused: argv)
    with pytest.raises(HandoffError):
        subject._argv(123, Path('/runtime/script.py'), subject.CHILD)


def test_scalar_projection_contains_only_closed_flags(monkeypatch):
    monkeypatch.setattr(subject, '_bounded', lambda *unused:
        b'/usr/bin/python3\0/runtime/script.py\0--sources\0newmark\0--admit-baseline-hold-additively\0')
    assert subject._argv(123, Path('/runtime/script.py'), subject.CHILD) == {
        'sources': 'newmark', 'admit-baseline-hold-additively': True}

@pytest.fixture
def native():
    guid = '{00000000-0000-0000-0000-000000000001}'
    task_path = subject.TASK_PATH + subject.TASKS['newmark']
    principal = 'S-1-5-21-example'
    events = []
    for number, event in enumerate((107, 129, 100, 200), 1):
        fields = {'TaskName': task_path}
        if event == 129:
            fields['ProcessID'] = '222'
        elif event == 200:
            fields.update(TaskInstanceId=guid, EnginePID='222', ActionName='powershell.exe')
        else:
            fields['InstanceId'] = guid
        if event == 100:
            fields['UserContext'] = principal
        data = ''.join('<Data Name="' + key + '">' + value + '</Data>' for key, value in fields.items())
        events.append('<Event xmlns="' + subject.NS['e'] + '"><System><Provider Name="Microsoft-Windows-TaskScheduler"/>'
                      '<Channel>Microsoft-Windows-TaskScheduler/Operational</Channel><Computer>HOST</Computer>'
                      '<EventID>' + str(event) + '</EventID><EventRecordID>' + str(number + 10) + '</EventRecordID>'
                      '<TimeCreated SystemTime="2026-10-25T03:00:0' + str(number) + 'Z"/></System>'
                      '<EventData>' + data + '</EventData></Event>')
    record = {'computer': 'HOST', 'started_at': '2026-10-25T03:00:00Z', 'finished_at': '2026-10-25T03:00:10Z',
              'watermark': {'record_id': 10, 'captured_at': '2026-10-25T03:00:00Z'}, 'upper_record_id': 14,
              'events': events, 'principals': [], 'tasks': [],
              'instances': [{'key': 'newmark', 'instance_guid': guid, 'engine_pid': 222}]}
    packet = {'task_baseline': []}
    for key in subject.TASKS:
        packet['task_baseline'].append({'key': key, 'xml': key + '-xml',
                                       'actions': [{'arguments': '-Sources ' + ('newmark' if key == 'newmark' else 'other')} ]})
        record['tasks'].append({'key': key, 'state': 'Running', 'actions': [{'execute': 'powershell.exe'}]})
        record['principals'].append({'key': key, 'user_id': principal})
    evidence = types.SimpleNamespace(raw=lambda role: ('<Task><Principals><Principal><UserId>' + principal
                                          + '</UserId></Principal></Principals></Task>').encode())
    stamp = int(subject.clock('2026-10-25T03:00:05Z').timestamp() * 1000)
    code = 'a' * 40
    bridge = {'task_instance_guid': guid, 'task_path': task_path, 'action_pid': 222,
              'action_creation_utc': '2026-10-25T03:00:01Z', 'direct_wsl_pid': 333, 'direct_wsl_parent_pid': 222,
              'direct_wsl_creation_utc': '2026-10-25T03:00:06Z', 'observed_at': '2026-10-25T03:00:07Z',
              'scheduler_event_record_ids': [11, 12, 13, 14],
              'scalar_argv': {'sources': 'newmark', 'expected-sha': code, 'windows-admission-unix-ms': str(stamp)}}
    def call():
        return subject._native(packet, record, bridge, 'newmark', code, stamp, evidence)
    return types.SimpleNamespace(**locals())


def test_native_original_instance_process_and_stamp_join(native):
    assert native.call() == 'newmark'


@pytest.mark.parametrize('mutation', ['foreign-pid', 'wrong-stamp', 'missing-event', 'outside-watermark', 'foreign-principal'])
def test_native_missing_or_foreign_bridge_refused(native, mutation):
    if mutation == 'foreign-pid':
        native.bridge['direct_wsl_parent_pid'] = 999
    elif mutation == 'wrong-stamp':
        native.bridge['scalar_argv']['windows-admission-unix-ms'] = '1'
    elif mutation == 'missing-event':
        native.record['events'].pop()
    elif mutation == 'outside-watermark':
        native.record['watermark']['record_id'] = 11
    else:
        native.record['principals'][1]['user_id'] = 'foreign'
    with pytest.raises(HandoffError):
        native.call()


def test_original_qualified_pipeline_scope_joins(active, monkeypatch):
    monkeypatch.setattr(subject, '_native', lambda *unused: 'qualified')
    active.args.update(sources='one,two', pipeline=True, **{'source-workers': '3'})
    active.pargs.update(sources='one,two', pipeline=True, **{'source-workers': '3'})
    active.manifest['config']['pipeline'] = True
    selected = active.manifest['sources'].pop('newmark')
    active.manifest['sources'].update(one=copy.deepcopy(selected), two=copy.deepcopy(selected))
    active.run_manifest['sources'] = {'one': {}, 'two': {}}
    active.bridge['scalar_argv']['sources'] = 'one,two'
    result = active.call()
    assert result['key'] == 'qualified'
    assert result['source'] == 'one,two'
    assert result['source_settlement'] == 'unverified'


def test_qualified_pipeline_cannot_widen_installed_scope(active, monkeypatch):
    monkeypatch.setattr(subject, '_native', lambda *unused: 'qualified')
    active.args.update(sources='newmark', pipeline=True)
    active.pargs.update(sources='newmark', pipeline=True)
    active.manifest['config']['pipeline'] = True
    with pytest.raises(HandoffError, match='task source selection differs'):
        active.call()
