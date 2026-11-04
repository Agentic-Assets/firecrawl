"""End-to-end synthetic failed-operation continuation with retained evidence.

Only Git/native process and Docker observations are synthetic boundaries. Original
A and both producer reviews, evidence hashing, and readers remain active.
"""
import copy
import json
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_cre_code_handoff as handoff
import test_cre_current_stack as current_stack_tests
import test_cre_maintenance_chain as chain
from continued_predecessor_fixture import produce_prior_failure
from current_stack_fixture import attach_current_stack, make_source

from cre_code_handoff_evidence import Evidence, sha

admitted = chain.admitted
lineage_case = chain.lineage_case
restoration_case = chain.restoration_case
qualified_ready_case = chain.qualified_ready_case
scheduler_principal_in_synthetic_xml = chain.scheduler_principal_in_synthetic_xml
current_inputs = current_stack_tests.current_inputs


@pytest.fixture
def complete_case(admitted, tmp_path):
    release, evidence, observation, inputs = handoff.complete_case.__wrapped__(admitted)
    release['roots']['runtime'] = str(tmp_path / 'runtime')
    return release, evidence, observation, inputs


def add_file_role(inputs, root, role, value):
    raw = value if isinstance(value, bytes) else handoff.encoded(value)
    path = root / (role + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    inputs.rows = [row for row in inputs.rows if row['role'] != role]
    inputs.rows.append({'role': role, 'path': str(path), 'bytes': len(raw), 'sha256': sha(raw)})
    inputs.data[str(path)] = raw
    return role


def bind_qa(packet, inputs, root, final, tooling, now, prefix):
    body = b'Independent synthetic QA review for exact test tree.'
    add_file_role(inputs, root, prefix + '-body', body)
    body_path = next(row['path'] for row in inputs.rows if row['role'] == prefix + '-body')
    add_file_role(inputs, root, prefix + '-review', handoff.attestation(
        {'qa_identity': final, 'tooling': tooling}, body_path, body, scope='synthetic-continuation'))
    log = b'Synthetic settled QA output.\n'
    add_file_role(inputs, root, prefix + '-log', log)
    gate = {'schema': 'cre-owned-gate-v1', 'producer': 'checkpoint-run-command-v1', 'identity': final,
            'argv': ['synthetic-qa'], 'cwd': '/synthetic/qa', 'effective_cwd': '/synthetic/qa',
            'exit_code': 0, 'local_settlement': 'confirmed', 'unchanged': True, 'log_closed': True,
            'started_at': now.isoformat(), 'finished_at': now.isoformat(), 'command_log_sha256': sha(log)}
    add_file_role(inputs, root, prefix + '-gate', gate)
    packet['qa'] = {'producer': 'owned-gates-v1', 'scope': 'synthetic-continuation', 'final_executed': final,
                    'independent_review': prefix + '-body', 'review_attestation': prefix + '-review',
                    'gates': [{'name': 'fixture', 'receipt': prefix + '-gate', 'executed': final,
                               'argv': ['synthetic-qa'], 'cwd': '/synthetic/qa', 'command_log': prefix + '-log'}]}


@pytest.fixture
def continued_ready_case(qualified_ready_case, current_inputs, tmp_path, monkeypatch):
    """Compose the original A fixture with an actual file-backed current model."""
    import cre_code_handoff_git as git_module
    import cre_runtime_maintenance_build as build

    case = qualified_ready_case
    source = make_source(tmp_path)
    prior = copy.deepcopy(case.packet)
    prior['inputs'] = copy.deepcopy(case.inputs.rows)
    # Materialize the synthetic original runtime snapshot without rewriting its bytes.
    row = next(row for row in prior['inputs'] if row['role'] == 'runtime_snapshot')
    raw = case.inputs.data[row['path']]
    original = tmp_path / 'original-runtime-snapshot.json'
    original.write_bytes(raw)
    row['path'] = str(original)
    prior_paths = ['scripts/firecrawl-ops/cre_collector/package-lock.json',
                   'scripts/firecrawl-ops/cre_collector/package.json']
    encoded_paths = b''.join(path.encode() + b'\0' for path in prior_paths)
    add_file_role(case.inputs, tmp_path / 'prior-qa', 'runtime_allowed_paths_nul', encoded_paths)
    add_file_role(case.inputs, tmp_path / 'prior-qa', 'native_allowed_paths_nul', encoded_paths)
    prior_scope = {'deployed': case.old, 'selected': source.prior, 'allowed_paths': prior_paths,
                   'objects': {name: {'before': None, 'after': {'mode': '100644', 'type': 'blob', 'oid': 'f' * 40}}
                               for name in prior_paths}, 'changed_dependency_paths': prior_paths}
    prior.update(native_change=copy.deepcopy(prior_scope), runtime_change=copy.deepcopy(prior_scope),
                 install_scope=['collector'])
    bind_qa(prior, case.inputs, tmp_path / 'prior-qa', source.prior, prior['tooling'], case.now, 'prior-qa')
    prior['inputs'] = copy.deepcopy(case.inputs.rows)
    row = next(row for row in prior['inputs'] if row['role'] == 'runtime_snapshot')
    row['path'] = str(original)
    # The failed A itself remains unchanged; Git's executable/path is the injected boundary.
    packet = copy.deepcopy(case.packet)
    development_root = packet['roots']['development']
    built = attach_current_stack(tmp_path, source, prior, packet, current_inputs,
                                 development_root=development_root)
    original_command = git_module.Git.command
    def command(self, repo, *args, **kwargs):
        return original_command(self, source.root if git_module.local_path(repo) == git_module.local_path(development_root) else repo,
                                *args, **kwargs)
    monkeypatch.setattr(git_module.Git, 'command', command)
    # The original A fixture uses invented SHAs; real Git ancestry has its own tests.
    monkeypatch.setattr(git_module.Git, 'maintenance_ancestry', lambda *_args: None)
    original_archive = build._source_archive
    monkeypatch.setattr(build, '_source_archive', lambda repo, identity, service:
                        original_archive(source.root if git_module.local_path(repo) == git_module.local_path(development_root) else repo,
                                         identity, service))
    def read(path):
        return case.inputs.data[path] if path in case.inputs.data else Path(path).read_bytes()

    prior_evidence = Evidence(prior, read)
    prior_runtime = prior_evidence.obj('runtime_snapshot')
    observation = copy.deepcopy(case.before)
    observation.update(containers=prior_runtime['containers'], native=case.old, runtime=case.old,
                       release_sha256=sha(built.prior_path.read_bytes()))
    prior_observation = tmp_path / 'failed-observation.json'
    prior_observation.write_bytes(handoff.encoded(observation))
    body = tmp_path / 'failed-review.md'
    body.write_bytes(b'Synthetic independent prior maintenance review.')
    review = tmp_path / 'failed-review.json'
    review.write_bytes(handoff.encoded(handoff.attestation(
        {'release_sha256': sha(built.prior_path.read_bytes()), 'tooling': prior['tooling']}, body,
        body.read_bytes(), scope='runtime-maintenance')))
    previous_output = tmp_path / 'failed-operation'
    auth = {'schema': 'cre-runtime-maintenance-authorization-v1', 'scope': 'held-runtime-maintenance',
            'release_sha256': sha(built.prior_path.read_bytes()), 'observation_sha256': sha(prior_observation.read_bytes()),
            'authorized_at': (case.now - timedelta(seconds=8)).isoformat(),
            'expires_at': (case.now + timedelta(seconds=200)).isoformat(), 'operation_output': str(previous_output),
            'review': {'path': str(review), 'sha256': sha(review.read_bytes()),
                       'reviewer': 'independent', 'approval_ref': 'AGENTIC-test'}}
    authorization = tmp_path / 'failed-authorization.json'
    authorization.write_bytes(handoff.encoded(auth))
    import cre_runtime_maintenance as maintenance
    maintenance.validate_maintenance_release(prior, prior_evidence)
    maintenance.validate_maintenance_authorization(built.prior_path.read_bytes(), prior, prior_evidence,
        prior_observation.read_bytes(), auth, case.now, output=previous_output)
    import cre_code_handoff as operation
    monkeypatch.setattr(operation, '__file__', str(Path(prior['roots']['tooling']) /
                        'scripts/firecrawl-ops/cre_collector/cre_code_handoff.py'))
    predecessor = produce_prior_failure(tmp_path, prior, built.prior_path, prior_observation, authorization,
                                        previous_output, prior_evidence, case.now, monkeypatch)
    for row in [*predecessor.roles, *built.roles]:
        case.inputs.rows = [item for item in case.inputs.rows if item['role'] != row['role']]
        case.inputs.rows.append(row)
    # The before snapshot is newly qualified, while historical_runtime_snapshot
    # and the original failed-A inputs still point to the original bytes.
    current_runtime = next(row for row in prior['inputs'] if row['role'] == 'runtime_snapshot')
    case.inputs.rows = [row for row in case.inputs.rows if row['role'] != 'runtime_snapshot']
    case.inputs.rows.append(current_runtime)
    packet['runtime_before'] = source.prior
    packet['native_before'] = source.prior
    packet['development'] = source.selected
    packet['build_source'] = {'root': str(tmp_path / 'selected-build'), 'identity': source.selected}
    packet['continuation'] = {**predecessor.continuation, 'current_stack': 'current_stack',
                              'current_stack_review': 'current_stack_review'}
    relative = 'scripts/firecrawl-ops/cre_collector/synthetic-ops-change.txt'
    actual_git = git_module.Git('unused')
    before_entries, after_entries = actual_git.entries(source.root, source.prior['sha']), actual_git.entries(source.root, source.selected['sha'])
    scope = {'deployed': source.prior, 'selected': source.selected, 'allowed_paths': [relative],
             'objects': {relative: {'before': before_entries.get(relative), 'after': after_entries.get(relative)}},
             'changed_dependency_paths': []}
    packet['native_change'] = copy.deepcopy(scope)
    packet['runtime_change'] = copy.deepcopy(scope)
    for role in ('native_allowed_paths_nul', 'runtime_allowed_paths_nul'):
        add_file_role(case.inputs, tmp_path / 'selected-roles', role, relative.encode() + b'\0')
    bind_qa(packet, case.inputs, tmp_path / 'selected-qa', source.selected, source.selected, case.now, 'selected-qa')
    packet['inputs'] = copy.deepcopy(case.inputs.rows)
    case.release_raw = handoff.encoded(packet)
    case.release.write_bytes(case.release_raw)
    case.before.update(containers=prior_runtime['containers'], native=source.prior, runtime=source.prior,
                       release_sha256=sha(case.release_raw))
    case.observation.write_bytes(handoff.encoded(case.before))
    new_review = case.authorization.parent / 'review.json'
    review_value = json.loads(new_review.read_bytes())
    review_value['subject'] = {'release_sha256': sha(case.release_raw), 'tooling': source.selected}
    new_review.write_bytes(handoff.encoded(review_value))
    new_auth = json.loads(case.authorization.read_bytes())
    new_auth.update(release_sha256=sha(case.release_raw), observation_sha256=sha(case.observation.read_bytes()))
    new_auth['review']['sha256'] = sha(new_review.read_bytes())
    case.authorization.write_bytes(handoff.encoded(new_auth))
    case.evidence = Evidence(packet, read)
    case.new = source.selected
    case.normalized = built.receipt['model']['reviewed_containers']
    case.before_identity = source.prior
    case.source = source
    case.built = built
    case.predecessor = predecessor
    case.prior = prior
    case.packet = packet
    return case


def test_continued_fixture_builds_real_prior_producer(continued_ready_case):
    import cre_runtime_maintenance as maintenance

    case = continued_ready_case
    maintenance.validate_maintenance_release(case.packet, case.evidence)
    assert case.predecessor.manifest.is_file()


@pytest.fixture
def continued_transition_case(continued_ready_case, tmp_path, monkeypatch):
    import cre_code_handoff as operation
    import cre_refresh_pipeline
    import cre_runtime_maintenance_transition as transition
    from cre_code_handoff_evidence import DEPENDENCY_CHECKS

    case = continued_ready_case
    monkeypatch.setattr(transition, 'validate_maintenance_git_history', lambda *_args: None)
    monkeypatch.setattr(transition, 'validate_maintenance_dependency_history', lambda *_args: None)
    monkeypatch.setattr(transition, 'validate_maintenance_tracking_history', lambda *_args: None)
    monkeypatch.setattr(cre_refresh_pipeline, 'command_poll_hook', lambda _poll: nullcontext())
    times = iter(case.now + timedelta(seconds=index) for index in range(30))
    monkeypatch.setattr(operation, 'now', lambda: next(times))
    checkpoint = SimpleNamespace(safe_process_env=dict,
        _run_logged_command=lambda argv, log, env: (log.write_bytes(b'synthetic settled output\n') and 0))
    runner = operation.Foreground(case.packet, checkpoint, case.output)
    runner.native('Hold', case.release, sha(case.release_raw), case.observation, case.authorization)
    collector = operation.local_path(case.packet['roots']['runtime']) / 'scripts/firecrawl-ops/cre_collector'
    for name, argv in DEPENDENCY_CHECKS.items():
        runner.run(name, ['bash', '-c', 'cd -- "$1" && shift && exec "$@"', 'cre-maintenance', str(collector), *argv],
                   effective_cwd=collector)
    for mode in ('Repin', 'Verify'):
        runner.native(mode, case.release, sha(case.release_raw), case.observation, case.authorization)
    _, ready, _ = chain.ready_payload_case.__wrapped__((case, collector))
    # Producer's retained Git witness must describe the actual preexisting BBC
    # identity, while original A and task XML retain their original old SHA.
    git_path = Path(ready['git_observation']['path'])
    git = json.loads(git_path.read_bytes())
    for key in ('native', 'runtime'):
        git[key]['before'].update(case.before_identity, tracking=case.before_identity['sha'])
    git_path.write_bytes(handoff.encoded(git))
    ready['git_observation'].update(bytes=git_path.stat().st_size, sha256=sha(git_path.read_bytes()))
    transition.validate_maintenance_ready(case.packet, case.evidence, ready, case.now + timedelta(seconds=23),
        release_raw=case.release_raw, release_path=case.release)
    return chain.transition_case.__wrapped__((case, ready, collector), tmp_path)


def test_failed_a_failed_bbc_current_model_ready_transition_and_restoration(continued_transition_case):
    from cre_code_restoration_evidence import validate_restoration
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, transition = continued_transition_case
    result = validate_maintenance_transition(transition, case.evidence.read)
    assert result['old'] == case.old and result['new'] == case.new
    assert case.packet['runtime_before'] == case.before_identity != case.old
    restoration = chain.restored_packet(case, transition)
    validate_restoration(restoration, Evidence(restoration, case.evidence.read))


@pytest.mark.parametrize('target', ['failed_command', 'recovery_archive', 'current_model', 'model_review', 'failed_a'])
def test_continued_transition_refuses_tampered_lineage(continued_transition_case, target):
    from cre_code_handoff_evidence import HandoffError
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, transition = continued_transition_case
    if target == 'failed_a':
        role = case.packet['failed_handoff']['failure']
        case.inputs.data[case.path(role)] = b'{"classification":"committed"}'
    else:
        paths = {'failed_command': Path(case.packet['continuation']['failed_operation']) / '06-compose-up.json',
                 'recovery_archive': case.predecessor.archive / 'known_outcome.evidence',
                 'current_model': Path(case.built.refs['current_stack']['path']),
                 'model_review': Path(case.built.refs['current_stack_review']['path'])}
        paths[target].write_bytes(b'foreign bytes')
    with pytest.raises(HandoffError):
        validate_maintenance_transition(transition, case.evidence.read)
