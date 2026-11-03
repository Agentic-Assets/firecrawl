"""Current-stack model compares synthetic Engine inputs without host access."""
import copy
import hashlib

import pytest

from cre_code_handoff_evidence import HandoffError
from cre_runtime_maintenance_images import snapshot_container


@pytest.fixture
def current_inputs():
    names = ('api', 'nuq-postgres', 'playwright-service', 'rabbitmq', 'redis')
    containers, images, configs = [], {}, {}
    expected = {}
    for name in names:
        image_id = 'sha256:' + hashlib.sha256(name.encode()).hexdigest()
        ident = hashlib.sha256(('container-' + name).encode()).hexdigest()
        image = {'Id': image_id, 'Config': {'Env': ['BASE=ok'], 'Labels': {},
                 'Cmd': ['serve'], 'Entrypoint': None, 'User': '', 'WorkingDir': '', 'ExposedPorts': {}}}
        labels = {'com.docker.compose.service': name, 'com.docker.compose.project': 'firecrawl'}
        config = {'Env': ['BASE=ok', 'TOKEN=private-fixture'], 'Labels': labels, 'Cmd': ['serve'],
                  'Entrypoint': None, 'User': '', 'WorkingDir': '', 'ExposedPorts': {}, 'Tty': False, 'Hostname': ident[:12]}
        from cre_current_stack import CONFIG_KEYS
        for field in CONFIG_KEYS - {'Healthcheck', 'StopSignal'}:
            config.setdefault(field, False if field in {'AttachStderr', 'AttachStdin', 'AttachStdout', 'OpenStdin', 'StdinOnce', 'Tty'} else None)
        config['Image'] = image_id
        host = {'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}, 'Memory': 0,
                'NanoCpus': 0, 'MemorySwap': 0, 'PidsLimit': None, 'ShmSize': 0, 'ReadonlyRootfs': False,
                'Privileged': False, 'SecurityOpt': None, 'CapAdd': None, 'CapDrop': None,
                'PortBindings': {}, 'NetworkMode': 'firecrawl_backend', 'CpuPeriod': 0, 'CpuQuota': 0,
                'CpuRealtimePeriod': 0, 'CpuRealtimeRuntime': 0, 'CpuShares': 0,
                'MemoryReservation': 0, 'BlkioWeight': 0, 'CpusetCpus': '', 'CpusetMems': '',
                'ExtraHosts': [], 'Ulimits': None, 'Tmpfs': None, 'LogConfig': {'Type': 'json-file', 'Config': {}}}
        from cre_current_stack import HOST_KEYS
        for field in HOST_KEYS - {'Tmpfs', 'Mounts'}:
            host.setdefault(field, None)
        nameval = '/firecrawl-' + name + '-1'
        row = {'Id': ident, 'Image': image_id, 'Name': nameval, 'State': {'Status': 'running', 'StartedAt': 'epoch'},
               'Config': config, 'HostConfig': host, 'Mounts': [],
               'NetworkSettings': {'Networks': {'firecrawl_backend': {'NetworkID': 'network', 'Aliases': [nameval[1:], name]}}}}
        containers.append(row); images[name] = image; expected[name] = image_id
        configs[name] = {'environment': {'TOKEN': 'private-fixture'}, 'networks': {'backend': None},
                         'logging': {'driver': 'json-file', 'options': {}}, 'restart': 'unless-stopped'}
    compose = {'name': 'firecrawl', 'services': configs, 'networks': {'backend': {'name': 'firecrawl_backend'}}, 'volumes': {}}
    packet = {'expected_images': expected, 'external_before': {r['Config']['Labels']['com.docker.compose.service']: snapshot_container(r)
              for r in containers if r['Config']['Labels']['com.docker.compose.service'] in ('redis', 'rabbitmq')},
              'reviewed_mounts': {name: [] for name in names}, 'reviewed_containers': [snapshot_container(r) for r in containers]}
    return containers, compose, images, packet


def qualifier():
    try:
        from cre_current_stack import qualify_current_stack
    except ImportError:
        pytest.fail('current-stack qualifier missing')
    return qualify_current_stack


def test_complete_current_model_is_private_and_bound_to_exact_fleet(current_inputs):
    result = qualifier()(*current_inputs)
    assert len(result['containers']) == 5
    assert result['historical_volume_continuity'] == 'unknown'
    assert 'private-fixture' not in repr(result)
    assert len(result['component_hashes']) == 5


@pytest.mark.parametrize('drift', ['privileged', 'capability', 'mount', 'network', 'port', 'environment', 'command', 'image', 'missing', 'identity', 'health', 'logging', 'tmpfs', 'ulimit', 'namespace', 'unknown', 'missing_required'])
def test_current_model_refuses_unreviewed_critical_drift(current_inputs, drift):
    containers, compose, images, packet = copy.deepcopy(current_inputs)
    api = containers[0]
    if drift == 'privileged': api['HostConfig']['Privileged'] = True
    elif drift == 'capability': api['HostConfig']['CapAdd'] = ['SYS_ADMIN']
    elif drift == 'mount': api['Mounts'] = [{'Type':'bind','Source':'/private','Destination':'/data','RW':True,'Propagation':'rprivate'}]
    elif drift == 'network': api['NetworkSettings']['Networks']['firecrawl_backend']['NetworkID'] = 'foreign'
    elif drift == 'port': api['HostConfig']['PortBindings'] = {'80/tcp':[{'HostIp':'0.0.0.0','HostPort':'80'}]}
    elif drift == 'environment': api['Config']['Env'][-1] = 'TOKEN=changed'
    elif drift == 'command': api['Config']['Cmd'] = ['unexpected']
    elif drift == 'image': api['Image'] = 'sha256:' + 'f'*64
    elif drift == 'identity': api['Id'] = 'a'*64; api['Config']['Hostname'] = 'a'*12
    elif drift == 'health': api['Config']['Healthcheck'] = {'Test': ['CMD', 'false']}
    elif drift == 'logging': api['HostConfig']['LogConfig']['Type'] = 'none'
    elif drift == 'tmpfs': api['HostConfig']['Tmpfs'] = {'/tmp': 'rw'}
    elif drift == 'ulimit': api['HostConfig']['Ulimits'] = [{'Name':'nofile','Soft':1,'Hard':1}]
    elif drift == 'namespace': api['HostConfig']['IpcMode'] = 'host'
    elif drift == 'unknown': api['HostConfig']['UnreviewedEngineControl'] = True
    elif drift == 'missing_required': del api['HostConfig']['IpcMode']
    else: del api['HostConfig']['SecurityOpt']
    if drift not in ('missing', 'identity'):
        packet['reviewed_containers'][0] = snapshot_container(api)
    with pytest.raises(HandoffError):
        qualifier()(containers, compose, images, packet)




def test_anonymous_volume_requires_reviewed_driver_and_identity(current_inputs):
    containers, compose, images, packet = copy.deepcopy(current_inputs)
    nuq = containers[1]
    nuq['Mounts'] = [{'Type': 'volume', 'Name': 'fixture-volume', 'Source': '/private-volume',
                      'Destination': '/data', 'RW': True, 'Propagation': '', 'Mode': '', 'Driver': 'local'}]
    images['nuq-postgres']['Config']['Volumes'] = {'/data': {}}
    nuq['Config']['Volumes'] = {'/data': {}}
    packet['reviewed_containers'][1] = snapshot_container(nuq)
    from cre_current_stack import _mounts
    packet['reviewed_mounts']['nuq-postgres'] = _mounts(nuq)
    with pytest.raises(HandoffError):
        qualifier()(containers, compose, images, packet)
    from cre_current_stack import _hash
    volume = {'Name': 'fixture-volume', 'Driver': 'local', 'Scope': 'local', 'CreatedAt': 'earlier',
              'Labels': {}, 'Options': {}}
    packet['reviewed_volumes'] = {_hash('fixture-volume'): {'Driver': 'local', 'Scope': 'local',
              'CreatedAt': 'earlier', 'labels_sha256': _hash({}), 'options_sha256': _hash({})}}
    result = qualifier()(containers, compose, images, packet, {'fixture-volume': volume})
    assert result['historical_volume_continuity'] == 'unknown'
    volume['Driver'] = 'other'
    with pytest.raises(HandoffError):
        qualifier()(containers, compose, images, packet, {'fixture-volume': volume})


def test_retained_current_model_refuses_missing_review():
    try:
        from cre_current_stack import validate_current_stack
    except ImportError:
        pytest.fail('current-stack retained validator missing')
    class Evidence:
        def raw(self, role):
            raise HandoffError('missing ' + role)
    with pytest.raises(HandoffError):
        validate_current_stack({'continuation': {'current_stack': 'current_stack',
                                'current_stack_review': 'current_stack_review',
                                'failed_release': 'failed_release'}}, Evidence(), {})


@pytest.fixture
def retained_case(current_inputs, monkeypatch):
    import json

    import cre_current_stack as module
    from cre_code_handoff_evidence import sha
    rows, compose, images, model = current_inputs
    model['reviewed_volumes'] = {}
    result = module.qualify_current_stack(rows, compose, images, model)
    prior = {'schema': 'cre-runtime-maintenance-v1', 'new': {'sha': 'b'*40, 'tree': 'c'*40},
             'expected_images': {k:model['expected_images'][k] for k in ('api','nuq-postgres','playwright-service')},
             'image_tags': {'api':'firecrawl-api:qa-fixture-0001'}, 'compose_files':['C:/fixture/base.yml','C:/fixture/private.yml'],
             'compose_override_path':'C:/fixture/reviewed.yml', 'native_docker_sha256':'d'*64,
             'native_compose': {'sha256':'e'*64}}
    overlay = module.reviewed_override(prior['image_tags'])
    source = {path:{'sha256':'f'*64,'bytes':1} for path in prior['compose_files']}
    source[prior['compose_override_path']] = {'sha256':sha(overlay),'bytes':len(overlay)}
    historical_source = copy.deepcopy(source)
    runtime_rows = [{'id': row['Id'], 'environment_sha256': sha(json.dumps(sorted(row['Config']['Env']), separators=(',', ':')).encode()),
                     'memory': row['HostConfig']['Memory'], 'nano_cpus': row['HostConfig']['NanoCpus'],
                     'cpu_quota': row['HostConfig']['CpuQuota'], 'cpu_period': row['HostConfig']['CpuPeriod']} for row in rows]
    compatibility = {row['Config']['Labels']['com.docker.compose.service']: module.verify_prior_environment_and_caps(
        old, row, images[row['Config']['Labels']['com.docker.compose.service']],
        images[row['Config']['Labels']['com.docker.compose.service']], {'TOKEN'}, [])
        for row, old in zip(rows, runtime_rows, strict=True)}
    prepared = b'fixture-prepared-images'
    prior_raw=json.dumps(prior,sort_keys=True).encode()
    receipt={'schema':'cre-current-stack-model-v1','prior_release_sha256':sha(prior_raw),
             'source_identity':prior['new'],'historical_volume_continuity':'unknown',
             'dependency_metadata':{name:{'desired_sha256':module._hash([]),'actual_sha256':module._hash([]),
                     'desired_count':0,'actual_count':0,'matches':True} for name in module.SERVICES},
             'tooling_identity':{'sha':'a'*40,'tree':'b'*40},'producer_sha256':sha(b'producer'),'model':model,
             'component_hashes':result['component_hashes'],'semantic_commitments':result['semantic_commitments'],
             'prior_compatibility':compatibility,
             'compose_sources':source,'native_docker_sha256':prior['native_docker_sha256'],
             'native_compose':prior['native_compose'],'prepared_images_sha256':sha(prepared)}
    class PriorEvidence:
        def __init__(self, release, read):
            self.original_payloads={}; self.original={}; self.inputs={}
        def raw(self,role):
            return {'compose_override':overlay,'prepared_images':prepared}[role]
        def obj(self,role):
            return {'container_config_before':model['reviewed_containers'], 'compose_sources':historical_source, 'runtime_snapshot':{'containers':runtime_rows}}[role]
        def recheck(self):
            pass
    class Git:
        def __init__(self, executable): pass
        def text(self, *args): return 'same-tree'
        def command(self, *args): return b'producer'
    monkeypatch.setattr(module,'Evidence',PriorEvidence)
    monkeypatch.setattr(module,'Git',Git)
    monkeypatch.setattr(module,'validate_prepared_image_history',lambda *args: None)
    packet={'continuation':{'current_stack':'current_stack','current_stack_review':'current_stack_review',
                            'failed_release':'failed_release'},'programs':{'native_git':'C:/git.exe'},
            'roots':{'development':'/fixture'},'new':{'sha':'a'*40,'tree':'b'*40},
            'tooling':{'sha':'a'*40,'tree':'b'*40}}
    body=b'independent current model review'
    class MemoryEvidence:
        def __init__(self):
            self.original_payloads={};self.inputs={'current_stack_review_body':{'path':'/fixture/review.txt'}}
            self.receipt=receipt
        def raw(self,role):
            data=json.dumps(self.receipt,sort_keys=True).encode()
            review={'schema':'cre-code-review-v1','subject':{'current_stack_sha256':sha(data),
                    'prior_release_sha256':sha(prior_raw)},'scope':'current-stack-baseline','verdict':'approved',
                    'reviewer':'reviewer','approval_ref':'AGENTIC-3752','historical_volume_continuity':'unknown',
                    'dependency_metadata_reviewed':True,
                    'body':{'path':'/fixture/review.txt','sha256':sha(body),'bytes':len(body)}}
            return {'current_stack':data,'current_stack_review':json.dumps(review).encode(),
                    'current_stack_review_body':body,'failed_release':prior_raw}[role]
        def obj(self,role):
            return self.receipt['model']['reviewed_containers'] if role=='container_config_before' else json.loads(self.raw(role))
        def read(self,path): raise AssertionError(path)
        def recheck(self): pass
    return module,packet,MemoryEvidence()


def test_retained_approved_model_binds_review_and_prior_image_boundary(retained_case):
    module,packet,evidence=retained_case
    assert module.validate_current_stack(packet,evidence,evidence.receipt) == evidence.receipt


@pytest.mark.parametrize('drift',['missing_commitment','mismatched_commitment','prior_identity','source','old_volume_claim'])
def test_retained_model_refuses_incomplete_or_forged_reviewed_evidence(retained_case,drift):
    module,packet,evidence=retained_case
    r=evidence.receipt
    if drift=='missing_commitment': del r['semantic_commitments']['api']['healthcheck']
    elif drift=='mismatched_commitment': r['semantic_commitments']['api']['ports']['actual_sha256']='0'*64
    elif drift=='prior_identity': r['source_identity']={'sha':'9'*40,'tree':'9'*40}
    elif drift=='source': r['compose_sources']['C:/fixture/base.yml']['sha256']='0'*64
    else:r['historical_volume_continuity']='verified'
    with pytest.raises(HandoffError):module.validate_current_stack(packet,evidence,r)




def test_preview_never_constructs_host_adapter(monkeypatch, capsys):
    import json

    import cre_current_stack as module
    raw = json.dumps({'schema': 'cre-runtime-maintenance-v1'}).encode()
    monkeypatch.setattr(module, 'read_file', lambda path: raw)
    monkeypatch.setattr(module, 'capture_current_stack', lambda *args: pytest.fail('host adapter invoked'))
    module.main(['--prior-release', '/fixture/release.json', '--output', '/fixture/output'])
    assert json.loads(capsys.readouterr().out)['mode'] == 'preview'








@pytest.mark.parametrize('field', ['verdict', 'scope', 'reviewer', 'body_hash', 'history_ack', 'dependency_ack', 'producer_hash'])
def test_retained_model_rejects_invalid_review_or_producer(retained_case, field):
    import json
    module, packet, evidence = retained_case
    if field == 'producer_hash':
        evidence.receipt['producer_sha256'] = '0' * 64
    else:
        original = evidence.raw
        def altered(role):
            data = original(role)
            if role != 'current_stack_review':
                return data
            review = json.loads(data)
            if field == 'body_hash':
                review['body']['sha256'] = '0' * 64
            elif field == 'history_ack':
                review['historical_volume_continuity'] = 'verified'
            elif field == 'dependency_ack':
                review['dependency_metadata_reviewed'] = False
            else:
                review[field] = ''
            return json.dumps(review).encode()
        evidence.raw = altered
    with pytest.raises(HandoffError):
        module.validate_current_stack(packet, evidence, evidence.receipt)


def test_capture_refuses_untrusted_tooling_before_docker(monkeypatch, tmp_path):
    import json

    import cre_current_stack as module
    prior = {'schema': 'cre-runtime-maintenance-v1', 'programs': {'native_git': 'C:/git.exe'},
             'roots': {'tooling': '/fixture'}}
    class Git:
        def __init__(self, executable): pass
        def clean(self, *args, **kwargs): raise HandoffError('untrusted tooling')
    monkeypatch.setattr(module, 'Git', Git)
    monkeypatch.setattr(module, '_docker_run', lambda *args, **kwargs: pytest.fail('Docker called'))
    with pytest.raises(HandoffError, match='untrusted tooling'):
        module.capture_current_stack(json.dumps(prior).encode(), prior, object(), tmp_path / 'capture',
                                     tooling_identity={'sha': 'a'*40, 'tree': 'b'*40})
    assert not (tmp_path / 'capture').exists()


@pytest.mark.parametrize('field', ['pid', 'devices', 'group_add', 'ipc', 'userns_mode', 'runtime', 'cpu_shares'])
def test_current_model_rejects_unmodeled_compose_directives(current_inputs, field):
    containers, compose, images, packet = copy.deepcopy(current_inputs)
    compose['services']['api'][field] = 'unreviewed-setting'
    with pytest.raises(HandoffError):
        qualifier()(containers, compose, images, packet)


@pytest.mark.parametrize('kind', ['port_option', 'network_option', 'volume_option'])
def test_current_model_rejects_unsupported_nested_compose_options(current_inputs, kind):
    containers, compose, images, packet = copy.deepcopy(current_inputs)
    api = compose['services']['api']
    if kind == 'port_option':
        api['ports'] = [{'target': 80, 'published': '80', 'protocol': 'tcp', 'app_protocol': 'http'}]
    elif kind == 'network_option':
        api['networks']['backend'] = {'ipv4_address': '192.0.2.1'}
    else:
        api['volumes'] = [{'type': 'volume', 'source': 'x', 'target': '/data', 'volume': {'subpath': 'other'}}]
    with pytest.raises(HandoffError):
        qualifier()(containers, compose, images, packet)




def test_dependency_metadata_surfaces_targeted_subset_without_claiming_old_config():
    from cre_current_stack import _dependency_metadata
    service = {'depends_on': {'redis': {'condition':'service_started', 'restart':False, 'required':True},
                              'nuq-postgres': {'condition':'service_healthy', 'restart':False, 'required':True}}}
    result = _dependency_metadata(service, {'com.docker.compose.depends_on':'nuq-postgres:service_healthy:false'})
    assert result['desired_count'] == 2
    assert result['actual_count'] == 1
    assert result['matches'] is False
    assert result['desired_sha256'] != result['actual_sha256']


def test_prior_environment_reconstruction_rejects_changed_explicit_secret():
    import json

    from cre_code_handoff_evidence import sha
    try:
        from cre_current_stack import verify_prior_environment_and_caps
    except ImportError:
        pytest.fail('prior environment and caps verifier missing')
    old_env=['TOKEN=original-private', 'NODE_VERSION=old']
    current_env=['TOKEN=original-private', 'NODE_VERSION=new']
    prior={'environment_sha256':sha(json.dumps(sorted(old_env), separators=(',', ':')).encode()),
           'memory':8,'nano_cpus':1,'cpu_quota':0,'cpu_period':0}
    current={'Config':{'Env':current_env},'HostConfig':{'Memory':8,'NanoCpus':1,'CpuQuota':0,'CpuPeriod':0}}
    old_image={'Config':{'Env':['NODE_VERSION=old']}}
    new_image={'Config':{'Env':['NODE_VERSION=new']}}
    result=verify_prior_environment_and_caps(prior,current,old_image,new_image,{'TOKEN'},['NODE_VERSION'])
    assert 'original-private' not in repr(result)
    current['Config']['Env'][0]='TOKEN=changed-private'
    with pytest.raises(HandoffError):
        verify_prior_environment_and_caps(prior,current,old_image,new_image,{'TOKEN'},['NODE_VERSION'])



def test_file_backed_model_uses_real_evidence_and_git_history(tmp_path, current_inputs):
    import current_stack_fixture as fixtures

    from cre_current_stack import validate_current_stack
    source = fixtures.make_source(tmp_path)
    prior = {'schema': 'cre-runtime-maintenance-v1', 'roots': {'native': 'C:/fixture/native'}, 'inputs': []}
    packet = {'continuation': {'failed_release': 'failed_release', 'current_stack': 'current_stack',
                               'current_stack_review': 'current_stack_review'}}
    built = fixtures.attach_current_stack(tmp_path, source, prior, packet, current_inputs)
    evidence = built.evidence(built.roles)
    assert validate_current_stack(packet, evidence, evidence.obj('current_stack')) == built.receipt
    assert evidence.original_payloads
    evidence.recheck()


def test_file_backed_model_rechecks_historical_image_artifact(tmp_path, current_inputs):
    import json
    from pathlib import Path

    import current_stack_fixture as fixtures

    from cre_current_stack import validate_current_stack
    source = fixtures.make_source(tmp_path)
    prior = {'schema': 'cre-runtime-maintenance-v1', 'roots': {'native': 'C:/fixture/native'}, 'inputs': []}
    packet = {'continuation': {'failed_release': 'failed_release', 'current_stack': 'current_stack',
                               'current_stack_review': 'current_stack_review'}}
    built = fixtures.attach_current_stack(tmp_path, source, prior, packet, current_inputs)
    evidence = built.evidence(built.roles)
    validate_current_stack(packet, evidence, evidence.obj('current_stack'))
    manifest_path = next(row['path'] for row in prior['inputs'] if row['role'] == 'prepared_images')
    manifest = json.loads(Path(manifest_path).read_bytes())
    Path(manifest['images'][0]['log']['path']).write_bytes(b'tampered')
    with pytest.raises(HandoffError):
        evidence.recheck()



def test_file_backed_model_preserves_synthetic_native_development_role(tmp_path, current_inputs, monkeypatch):
    import current_stack_fixture as fixtures

    import cre_runtime_maintenance_build as build
    from cre_code_handoff_git import Git
    from cre_current_stack import validate_current_stack
    source = fixtures.make_source(tmp_path)
    prior = {'schema': 'cre-runtime-maintenance-v1', 'roots': {'native': 'C:/fixture/native'}, 'inputs': []}
    packet = {'continuation': {'failed_release': 'failed_release', 'current_stack': 'current_stack',
                               'current_stack_review': 'current_stack_review'}}
    built = fixtures.attach_current_stack(tmp_path, source, prior, packet, current_inputs, development_root='C:/dev')
    original_command, original_archive = Git.command, build._source_archive
    def mapped_command(self, repo, *args, **kwargs):
        return original_command(self, source.root if str(repo).replace('\\', '/').endswith('/dev') else repo,
                                *args, **kwargs)
    def mapped_archive(repo, selected, service):
        return original_archive(source.root, selected, service)
    monkeypatch.setattr(Git, 'command', mapped_command)
    monkeypatch.setattr(build, '_source_archive', mapped_archive)
    evidence = built.evidence(built.roles)
    assert packet['roots']['development'] == 'C:/dev'
    assert validate_current_stack(packet, evidence, evidence.obj('current_stack')) == built.receipt
