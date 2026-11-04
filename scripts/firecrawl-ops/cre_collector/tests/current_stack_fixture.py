"""File-backed synthetic current-stack evidence for continuation integration tests.

Docker image build/inspect receipts are synthetic external boundaries. Git objects,
source archives, all Evidence inputs and review hashes are real temporary files.
This helper never contacts Docker, providers, Scheduler, or a database.
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from cre_code_handoff_evidence import Evidence, sha
from cre_current_stack import (
    COMMITMENT_FIELDS,
    qualify_current_stack,
    verify_prior_environment_and_caps,
)
from cre_runtime_maintenance_build import (
    ENGINE,
    SERVICES,
    _context_tar,
    _source_archive,
    reviewed_override,
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else encoded(value)
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': sha(raw), 'bytes': len(raw)}


def make_source(tmp_path):
    """Create actual two-commit Git history; image context trees do not change."""
    root = Path(tmp_path) / 'current-stack-git'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout.decode().strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Synthetic Test')
    git('config', 'user.email', 'synthetic@example.invalid')
    source = Path(__file__).resolve().parents[1] / 'cre_current_stack.py'
    module = root / 'scripts/firecrawl-ops/cre_collector/cre_current_stack.py'
    module.parent.mkdir(parents=True)
    shutil.copyfile(source, module)
    windows = module.parent / 'windows'
    windows.mkdir()
    for name in ('CreCodeHandoff.ps1', 'CreTaskContract.ps1', 'CreRestorationExecution.ps1',
                 'CreRuntimeMaintenance.ps1', 'CreNativeCapture.cs'):
        (windows / name).write_text('synthetic committed helper: ' + name + '\n', encoding='utf-8')
    for path in SERVICES.values():
        dockerfile = root / path / 'Dockerfile'
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text('FROM scratch\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-m', 'synthetic prior')
    prior = {'sha': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}')}
    (module.parent / 'synthetic-ops-change.txt').write_text('new\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-m', 'synthetic continuation')
    selected = {'sha': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}')}
    return SimpleNamespace(root=root, prior=prior, selected=selected, git=git)


def attach_current_stack(tmp_path, source, prior, packet, current_inputs, *, development_root=None):
    """Seal prior+review to real files, returning role refs for caller's Evidence.

    Caller supplies actual synthetic Docker rows/model and an unsealed prior
    release whose original A inputs already refer to real files. This mutates
    `prior` to update build/source/runtime roles and sets `prior.new`; it sets
    continuation packet's development/tooling identities to the two Git commits.
    Returned `prior_path` must be used by the failure manifest and seven command
    records before those predecessor records are sealed.
    """
    rows, compose, images, model = current_inputs
    model['reviewed_volumes'] = {}
    result = qualify_current_stack(rows, compose, images, model)
    prior['new'] = source.prior
    development_root = str(source.root) if development_root is None else str(development_root)
    prior.setdefault('roots', {})['development'] = development_root
    prior.setdefault('programs', {})['native_docker'] = 'C:/fixture/docker.exe'
    prior['native_docker_sha256'] = 'd' * 64
    prior['native_compose'] = {'path': 'C:/fixture/compose.exe', 'sha256': 'e' * 64, 'aliases': []}
    prior['build_source'] = {'root': str(Path(tmp_path) / 'build-source'), 'identity': source.prior}
    prior['expected_images'] = {name: model['expected_images'][name] for name in SERVICES}
    prior['image_tags'] = {name: 'firecrawl-' + name + ':qa-fixture-0001' for name in SERVICES}
    prior['compose_files'] = ['C:/fixture/base.yml', 'C:/fixture/private.yml']
    prior['compose_override_path'] = 'C:/fixture/reviewed.yml'
    prior.setdefault('inputs', [])
    original_runtime = next((row for row in prior['inputs'] if row['role'] == 'runtime_snapshot'), None)
    runtime_body = json.loads(Path(original_runtime['path']).read_bytes()) if original_runtime else {}
    if original_runtime and not any(row['role'] == 'historical_runtime_snapshot' for row in prior['inputs']):
        prior['inputs'].append({**original_runtime, 'role': 'historical_runtime_snapshot'})
    def role(name, value):
        prior['inputs'] = [row for row in prior['inputs'] if row['role'] != name]
        ref = write(Path(tmp_path) / 'prior-inputs' / (name + '.json'), value)
        prior['inputs'].append({'role': name, **ref})
        return ref
    snapshot = model['reviewed_containers']
    role('container_config_before', snapshot)
    runtime_rows = []
    for row in rows:
        runtime_rows.append({'id': row['Id'], 'name': row['Name'], 'image': row['Image'], 'status': 'running',
                             'started_at': row['State']['StartedAt'],
                             'environment_sha256': sha(json.dumps(sorted(row['Config']['Env']), separators=(',', ':')).encode()),
                             'memory': row['HostConfig']['Memory'], 'nano_cpus': row['HostConfig']['NanoCpus'],
                             'cpu_quota': row['HostConfig']['CpuQuota'], 'cpu_period': row['HostConfig']['CpuPeriod']})
    role('runtime_snapshot', {**runtime_body, 'containers': runtime_rows})
    overlay = reviewed_override(prior['image_tags'])
    role('compose_override', overlay)
    sources = {name: {'sha256': sha(name.encode()), 'bytes': len(name.encode())} for name in prior['compose_files']}
    role('compose_sources', sources)
    manifest_rows = []
    for service, context in SERVICES.items():
        archive = _source_archive(source.root, source.prior, service)
        artifacts = Path(tmp_path) / 'artifacts' / service
        log = write(artifacts / 'stdout.log', b'synthetic settled build stdout\n')
        stderr = write(artifacts / 'stderr.log', b'')
        image_id = prior['expected_images'][service]
        inspect = write(artifacts / 'inspect.json', {'Id': image_id, 'Config': {'Labels': {
            'org.opencontainers.image.revision': source.prior['sha']}}})
        tar = write(artifacts / 'context.tar', _context_tar(archive, service))
        docker = '/mnt/c/fixture/docker.exe'
        command = [docker, '--host', ENGINE, 'build', '--progress', 'plain', '--label',
                   'org.opencontainers.image.revision=' + source.prior['sha']]
        if service == 'api':
            command += ['--build-arg', 'GIT_SHA=' + source.prior['sha']]
        command += ['-t', prior['image_tags'][service], '-']
        manifest_rows.append({'service': service, 'identity': source.prior,
                              'tree': source.git('rev-parse', source.prior['sha'] + ':' + context),
                              'dockerfile': context + '/Dockerfile', 'tag': prior['image_tags'][service],
                              'image': image_id, 'exit_code': 0, 'stdout_eof': True, 'stderr_eof': True,
                              'log': log, 'stderr': stderr, 'inspect': inspect, 'context': tar,
                              'archive_sha256': sha(archive), 'command': command,
                              'started_at': '2026-11-02T00:00:00+00:00', 'finished_at': '2026-11-02T00:00:01+00:00'})
    manifest = {'schema': 'cre-prepared-images-v1', 'identity': source.prior,
                'source_root': prior['build_source']['root'], 'project_root': prior['roots']['native'],
                'docker_sha256': prior['native_docker_sha256'], 'images': manifest_rows}
    prepared_ref = role('prepared_images', manifest)
    prior_ref = write(Path(tmp_path) / 'failed-release.json', prior)
    compatibility = {}
    for row, old in zip(rows, runtime_rows, strict=True):
        name = row['Config']['Labels']['com.docker.compose.service']
        compatibility[name] = verify_prior_environment_and_caps(old, row, images[name], images[name],
                                                               set(compose['services'][name]['environment']), [])
    dependency = {name: {'desired_sha256': sha(b'[]'), 'actual_sha256': sha(b'[]'),
                         'desired_count': 0, 'actual_count': 0, 'matches': True} for name in model['expected_images']}
    receipt = {'schema': 'cre-current-stack-model-v1', 'prior_release_sha256': prior_ref['sha256'],
               'source_identity': source.prior, 'tooling_identity': source.selected,
               'producer_sha256': sha((source.root / 'scripts/firecrawl-ops/cre_collector/cre_current_stack.py').read_bytes()),
               'historical_volume_continuity': 'unknown', 'model': model,
               'component_hashes': result['component_hashes'], 'semantic_commitments': result['semantic_commitments'],
               'dependency_metadata': dependency, 'prior_compatibility': compatibility,
               'compose_sources': {**sources, prior['compose_override_path']: {'sha256': sha(overlay), 'bytes': len(overlay)}},
               'native_docker_sha256': prior['native_docker_sha256'], 'native_compose': prior['native_compose'],
               'prepared_images_sha256': prepared_ref['sha256']}
    assert set(next(iter(receipt['semantic_commitments'].values()))) == COMMITMENT_FIELDS
    model_ref = write(Path(tmp_path) / 'current-model.json', receipt)
    body = write(Path(tmp_path) / 'current-review.md', b'Synthetic review: prior volume unknown; dependency metadata reviewed.\n')
    review = {'schema': 'cre-code-review-v1', 'subject': {'current_stack_sha256': model_ref['sha256'],
              'prior_release_sha256': prior_ref['sha256']}, 'scope': 'current-stack-baseline', 'verdict': 'approved',
              'reviewer': 'independent-fixture', 'approval_ref': 'AGENTIC-3752', 'body': body,
              'historical_volume_continuity': 'unknown', 'dependency_metadata_reviewed': True}
    review_ref = write(Path(tmp_path) / 'current-review.json', review)
    baseline_ref = write(Path(tmp_path) / 'current-before.json', snapshot)
    refs = {'failed_release': prior_ref, 'current_stack': model_ref, 'current_stack_review': review_ref,
            'current_stack_review_body': body, 'container_config_before': baseline_ref}
    packet['new'] = source.selected
    packet['tooling'] = source.selected
    packet.setdefault('roots', {})['development'] = development_root
    packet.setdefault('programs', {})['native_git'] = 'C:/fixture/git.exe'
    return SimpleNamespace(prior=prior, prior_path=Path(prior_ref['path']), receipt=receipt,
                           refs=refs, roles=[{'role': name, **ref} for name, ref in refs.items()],
                           evidence=lambda inputs: Evidence({'inputs': inputs}, lambda path: Path(path).read_bytes()))
