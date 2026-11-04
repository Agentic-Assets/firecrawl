"""Qualify the current held Firecrawl fleet against an independently reviewed model.

Raw Docker environment and mount source data stay in process memory. This is not
proof of an earlier container's anonymous volume identity or database contents.
"""
import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path

from cre_code_handoff_evidence import (
    Evidence,
    identity,
    json_bytes,
    require,
    sha,
    unique,
    validate_review,
)
from cre_code_handoff_git import Git, local_path, read_file
from cre_runtime_maintenance_build import (
    _docker_run,
    reviewed_override,
    validate_prepared_image_history,
    verify_native_compose,
    verify_native_docker,
)
from cre_runtime_maintenance_images import (
    qualify_environment_transition,
    snapshot_container,
)

SERVICES = frozenset({'api', 'nuq-postgres', 'playwright-service', 'rabbitmq', 'redis'})
CONTROLS = ('rabbitmq', 'redis')
HOST_KEYS = frozenset(['AutoRemove', 'Binds', 'BlkioDeviceReadBps', 'BlkioDeviceReadIOps', 'BlkioDeviceWriteBps', 'BlkioDeviceWriteIOps', 'BlkioWeight', 'BlkioWeightDevice', 'CapAdd', 'CapDrop', 'Cgroup', 'CgroupParent', 'CgroupnsMode', 'ConsoleSize', 'ContainerIDFile', 'CpuCount', 'CpuPercent', 'CpuPeriod', 'CpuQuota', 'CpuRealtimePeriod', 'CpuRealtimeRuntime', 'CpuShares', 'CpusetCpus', 'CpusetMems', 'DeviceCgroupRules', 'DeviceRequests', 'Devices', 'Dns', 'DnsOptions', 'DnsSearch', 'ExtraHosts', 'GroupAdd', 'IOMaximumBandwidth', 'IOMaximumIOps', 'IpcMode', 'Isolation', 'Links', 'LogConfig', 'MaskedPaths', 'Memory', 'MemoryReservation', 'MemorySwap', 'MemorySwappiness', 'NanoCpus', 'NetworkMode', 'OomKillDisable', 'OomScoreAdj', 'PidMode', 'PidsLimit', 'PortBindings', 'Privileged', 'PublishAllPorts', 'ReadonlyPaths', 'ReadonlyRootfs', 'RestartPolicy', 'Runtime', 'SecurityOpt', 'ShmSize', 'UTSMode', 'Ulimits', 'UsernsMode', 'VolumeDriver', 'VolumesFrom'])
HOST_KEYS |= {'Tmpfs', 'Mounts'}
MODELED_HOST_KEYS = frozenset(['Binds', 'Mounts', 'Tmpfs', 'CapAdd', 'CapDrop', 'SecurityOpt', 'Dns', 'DnsOptions', 'DnsSearch', 'ExtraHosts', 'LogConfig', 'Memory', 'MemorySwap', 'NanoCpus', 'PidsLimit', 'PortBindings', 'Privileged', 'ReadonlyRootfs', 'RestartPolicy', 'ShmSize', 'Ulimits'])
CONFIG_KEYS = frozenset({'AttachStderr', 'AttachStdin', 'AttachStdout', 'Cmd', 'Domainname', 'Entrypoint',
                         'Env', 'ExposedPorts', 'Healthcheck', 'Hostname', 'Image', 'Labels', 'OpenStdin',
                         'StdinOnce', 'StopSignal', 'Tty', 'User', 'Volumes', 'WorkingDir'})
COMMITMENT_FIELDS = frozenset({'environment', 'command', 'execution_identity', 'healthcheck', 'ports',
                                'exposed_ports', 'security', 'mounts', 'extra_hosts', 'ulimits', 'tmpfs', 'logging',
                                'image', 'terminal', 'dns', 'network', 'namespace', 'resources', 'restart', 'volumes',
                                'runtime_config', 'labels'})
SERVICE_KEYS = frozenset({'build', 'command', 'cpus', 'depends_on', 'entrypoint', 'environment', 'extra_hosts',
                          'image', 'logging', 'mem_limit', 'memswap_limit', 'networks', 'ports', 'restart', 'ulimits',
                          'volumes', 'cap_drop', 'cap_add', 'healthcheck', 'pids_limit', 'security_opt', 'shm_size',
                          'tmpfs', 'privileged', 'read_only', 'dns', 'dns_search', 'dns_opt', 'tty', 'stdin_open',
                          'hostname', 'domainname', 'stop_signal', 'user', 'working_dir', 'labels'})


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _env(rows):
    require(isinstance(rows, list), 'environment list missing')
    result = {}
    for item in rows:
        require(isinstance(item, str) and '=' in item, 'invalid environment')
        key, value = item.split('=', 1)
        require(key and key not in result, 'duplicate environment key')
        result[key] = value
    return result


def _ports(service):
    result = {}
    for row in service.get('ports') or []:
        require(isinstance(row, dict) and {'target', 'published', 'protocol'} <= set(row)
                and set(row) <= {'target', 'published', 'protocol', 'host_ip', 'mode'}
                and row.get('mode', 'ingress') == 'ingress', 'unsupported port')
        key = str(row['target']) + '/' + row['protocol']
        result.setdefault(key, []).append({'HostIp': row.get('host_ip', ''), 'HostPort': str(row['published'])})
    return result


def _mounts(row):
    result = []
    for mount in row['Mounts']:
        require(isinstance(mount, dict) and {'Type', 'Source', 'Destination', 'RW', 'Propagation', 'Mode', 'Driver'} <= set(mount)
                and type(mount['RW']) is bool, 'mount inspection incomplete')
        result.append({'type': mount['Type'], 'source_sha256': _hash(mount['Source']),
                       'name_sha256': _hash(mount.get('Name')), 'destination_sha256': _hash(mount['Destination']),
                       'rw': mount['RW'], 'propagation': mount['Propagation'], 'mode': mount['Mode'],
                       'driver': mount['Driver']})
    return sorted(result, key=lambda value: json.dumps(value, sort_keys=True))


def _volume_summary(volume):
    require(isinstance(volume, dict) and all(key in volume for key in
            ('Name', 'Driver', 'Scope', 'CreatedAt', 'Labels', 'Options')), 'volume metadata incomplete')
    result = {field: volume[field] for field in ('Driver', 'Scope', 'CreatedAt')}
    result.update(labels_sha256=_hash(volume['Labels']), options_sha256=_hash(volume['Options']))
    return result


def _duration(value):
    match = re.fullmatch(r'(\d+)(ns|us|µs|ms|s|m|h)', str(value))
    require(match is not None, 'unsupported healthcheck duration')
    return int(match.group(1)) * {'ns': 1, 'us': 1000, 'µs': 1000, 'ms': 1000000,
                                  's': 1000000000, 'm': 60000000000, 'h': 3600000000000}[match.group(2)]


def _health(service, defaults):
    health = service.get('healthcheck')
    if health is None:
        return defaults.get('Healthcheck')
    require(isinstance(health, dict) and set(health) <= {'test', 'interval', 'timeout', 'start_period',
                                                          'start_interval', 'retries'}, 'unsupported healthcheck')
    result = {}
    for source, dest in (('test', 'Test'), ('interval', 'Interval'), ('timeout', 'Timeout'),
                         ('start_period', 'StartPeriod'), ('start_interval', 'StartInterval'), ('retries', 'Retries')):
        if source in health:
            value = health[source]
            if source == 'test':
                require(isinstance(value, list) and all(isinstance(x, str) for x in value), 'invalid healthcheck')
                value = [x.replace('$$', '$') for x in value]
            elif source != 'retries':
                value = _duration(value)
            result[dest] = value
    return result


def _dependency_metadata(service, labels):
    dependencies = service.get('depends_on') or {}
    require(isinstance(dependencies, dict), 'unsupported Compose dependency graph')
    desired = []
    for name, value in dependencies.items():
        require(isinstance(name, str) and isinstance(value, dict)
                and set(value) <= {'condition', 'restart', 'required'}
                and value.get('required', True) is True and type(value.get('restart', False)) is bool,
                'unsupported Compose dependency')
        desired.append([name, value.get('condition', 'service_started'), value.get('restart', False)])
    label = labels.get('com.docker.compose.depends_on')
    require(isinstance(label, str), 'Compose dependency label missing')
    actual = []
    for item in label.split(',') if label else []:
        parts = item.split(':')
        require(len(parts) == 3 and parts[0] and parts[1] and parts[2] in {'true', 'false'},
                'unsupported Compose dependency label')
        actual.append([parts[0], parts[1], parts[2] == 'true'])
    desired.sort()
    actual.sort()
    return {'desired_sha256': _hash(desired), 'actual_sha256': _hash(actual),
            'desired_count': len(desired), 'actual_count': len(actual), 'matches': desired == actual}


def verify_prior_environment_and_caps(prior, current, old_image, new_image, explicit_keys, allowed_keys):
    """Reconstruct only reviewed image defaults in memory and bind prior caps."""
    require(isinstance(allowed_keys, list) and len(allowed_keys) == len(set(allowed_keys))
            and isinstance(explicit_keys, set), 'prior environment scope malformed')
    current_env = _env(current['Config']['Env'])
    old_defaults = _env(old_image['Config'].get('Env') or [])
    reconstructed = dict(current_env)
    for key in allowed_keys:
        require(key in reconstructed and key in old_defaults, 'reviewed prior image default missing')
        reconstructed[key] = old_defaults[key]
    old_rows = [key + '=' + value for key, value in reconstructed.items()]
    qualify_environment_transition(old_rows, current['Config']['Env'], old_image['Config'].get('Env') or [],
                                   new_image['Config'].get('Env') or [], explicit_keys, set(allowed_keys))
    old_digest = sha(json.dumps(sorted(old_rows), separators=(',', ':')).encode())
    require(old_digest == prior.get('environment_sha256'), 'prior explicit environment differs')
    pairs = (('memory', 'Memory'), ('nano_cpus', 'NanoCpus'), ('cpu_quota', 'CpuQuota'), ('cpu_period', 'CpuPeriod'))
    expected = [prior.get(left) for left, _ in pairs]
    actual = [current['HostConfig'].get(right) for _, right in pairs]
    require(all(type(x) is int for x in expected + actual) and expected == actual, 'prior resource limits differ')
    return {'environment': {'expected_sha256': prior['environment_sha256'], 'actual_sha256': old_digest},
            'resources': {'expected_sha256': _hash(expected), 'actual_sha256': _hash(actual)}}


def qualify_current_stack(containers, compose, images, packet, volume_inspects=None):
    """Compare current in-memory inspect with Compose, images, and reviewed identity.

    Packet reviewed_containers and reviewed_mounts must be independently reviewed
    inputs, not constructed from the same observation at the apply boundary.
    """
    require(isinstance(containers, list) and len(containers) == 5, 'five containers required')
    rows = {row['Config']['Labels']['com.docker.compose.service']: row for row in containers}
    require(len(rows) == 5 and set(rows) == SERVICES, 'current service fleet differs')
    require(isinstance(compose, dict) and compose.get('name') == 'firecrawl'
            and set(compose.get('services', {})) >= SERVICES and isinstance(images, dict)
            and set(images) == SERVICES, 'current model inputs incomplete')
    snapshots = [snapshot_container(rows[name]) for name in sorted(SERVICES)]
    reviewed = unique(packet.get('reviewed_containers'), 'service')
    require(set(reviewed) == SERVICES and all(reviewed[x['service']] == x for x in snapshots),
            'reviewed current identities or configuration changed')
    external = packet.get('external_before')
    require(isinstance(external, dict) and set(external) == set(CONTROLS)
            and all(external[name] == snapshot_container(rows[name]) for name in CONTROLS),
            'unrelated service changed')
    image_ids = packet.get('expected_images')
    require(isinstance(image_ids, dict) and set(image_ids) == SERVICES, 'reviewed image scope differs')
    mount_review = packet.get('reviewed_mounts')
    require(isinstance(mount_review, dict) and set(mount_review) == SERVICES, 'reviewed mounts missing')
    volume_inspects = {} if volume_inspects is None else volume_inspects
    reviewed_volumes = packet.get('reviewed_volumes', {})
    require(isinstance(volume_inspects, dict) and isinstance(reviewed_volumes, dict), 'volume evidence missing')
    observed_volumes = {}
    hashes, commitments = {}, {}
    for name in sorted(SERVICES):
        row, service, image = rows[name], compose['services'][name], images[name]
        require(isinstance(service, dict) and set(service) <= SERVICE_KEYS, 'unsupported Compose service directive')
        cfg, host = row['Config'], row['HostConfig']
        require(set(host) <= HOST_KEYS and HOST_KEYS - {'Mounts', 'Tmpfs'} <= set(host),
                'unsupported or missing Engine host control')
        require(set(cfg) <= CONFIG_KEYS and CONFIG_KEYS - {'Healthcheck', 'StopSignal'} <= set(cfg),
                'unsupported or missing Engine config control')
        require(all(type(cfg[key]) is bool for key in
                    ('AttachStderr', 'AttachStdin', 'AttachStdout', 'OpenStdin', 'StdinOnce', 'Tty')),
                'invalid Engine stream or terminal control')
        defaults = image.get('Config')
        require(isinstance(defaults, dict) and row['Image'] == image.get('Id') == image_ids[name],
                'current image identity differs')
        require(row['State'].get('Status') == 'running' and row['State'].get('StartedAt'), 'service not running')
        health = row['State'].get('Health')
        require(health is None or health.get('Status') == 'healthy', 'service health differs')
        explicit = service.get('environment') or {}
        require(isinstance(explicit, dict) and all(isinstance(k, str) and v is not None for k, v in explicit.items()),
                'explicit environment unsupported')
        desired = {**_env(defaults.get('Env') or []), **{k: str(v) for k, v in explicit.items()}}
        require(_env(cfg.get('Env')) == desired, 'current environment differs')
        hostname = service.get('hostname')
        require(cfg.get('Hostname') == hostname if hostname else cfg.get('Hostname') in (row['Id'], row['Id'][:12]),
                'hostname differs')
        require((cfg.get('Domainname') or '') == (service.get('domainname') or ''), 'domain name differs')
        stop_signal = service.get('stop_signal')
        require((cfg.get('StopSignal') or '') == ((defaults.get('StopSignal') if stop_signal is None else stop_signal)
                                                 or ''), 'stop signal differs')
        require(set((cfg.get('Volumes') or {}).keys()) == set((defaults.get('Volumes') or {}).keys()),
                'image volume declaration differs')
        for key in ('AttachStderr', 'AttachStdin', 'AttachStdout', 'StdinOnce'):
            require(all(cfg.get(key) == rows[control]['Config'].get(key) for control in CONTROLS),
                    'attach or stdin mode differs from unchanged peers')
        require(cfg.get('Tty', False) == bool(service.get('tty', False))
                and cfg.get('OpenStdin', False) == bool(service.get('stdin_open', False)),
                'terminal or stdin setting differs')
        for key, value in (defaults.get('Labels') or {}).items():
            if key not in (service.get('labels') or {}):
                require(cfg['Labels'].get(key) == value, 'image label differs')
        require(all(cfg['Labels'].get(key) == str(value) for key, value in (service.get('labels') or {}).items()),
                'Compose label differs')
        for source, key in (('command', 'Cmd'), ('entrypoint', 'Entrypoint')):
            value = service.get(source)
            require(cfg.get(key) == (defaults.get(key) if value is None else value), 'current command differs')
        for source, key in (('user', 'User'), ('working_dir', 'WorkingDir')):
            value = service.get(source)
            require((cfg.get(key) or '') == ((defaults.get(key) if value is None else value) or ''),
                    'current execution identity differs')
        desired_health = _health(service, defaults)
        actual_health = cfg.get('Healthcheck')
        require((actual_health or {}) == (desired_health or {}), 'healthcheck differs')
        require((host.get('PortBindings') or {}) == _ports(service), 'published ports differ')
        exposed = set((defaults.get('ExposedPorts') or {}).keys()) | {
            str(x['target']) + '/' + x['protocol'] for x in service.get('ports') or []}
        require(set((cfg.get('ExposedPorts') or {}).keys()) == exposed, 'exposed ports differ')
        require(type(host.get('Privileged')) is bool and host['Privileged'] == bool(service.get('privileged', False))
                and type(host.get('ReadonlyRootfs')) is bool
                and host['ReadonlyRootfs'] == bool(service.get('read_only', False)), 'security mode differs')
        require(all(key in host for key in ('CapAdd', 'CapDrop', 'SecurityOpt'))
                and set(host['CapAdd'] or []) == set(service.get('cap_add') or [])
                and set(host['CapDrop'] or []) == set(service.get('cap_drop') or [])
                and set(host['SecurityOpt'] or []) == set(service.get('security_opt') or []),
                'capabilities or security options differ')
        for comp, field in (('dns', 'Dns'), ('dns_search', 'DnsSearch'), ('dns_opt', 'DnsOptions')):
            if field in host:
                require(sorted(host[field] or []) == sorted(service.get(comp) or []), 'DNS differs')
            else:
                require(not service.get(comp), 'DNS observation missing')
        require(all(value is None or value == {} for value in (service.get('networks') or {}).values()),
                'unsupported Compose network attachment options')
        expected_nets = {(compose.get('networks', {}).get(n) or {}).get('name', n)
                         for n in (service.get('networks') or {})}
        networks = row['NetworkSettings']['Networks']
        require(set(networks) == expected_nets, 'network membership differs')
        for net, detail in networks.items():
            require(all(net in rows[x]['NetworkSettings']['Networks']
                        and detail.get('NetworkID') == rows[x]['NetworkSettings']['Networks'][net].get('NetworkID')
                        for x in CONTROLS), 'network identity differs')
            require(set(detail.get('Aliases') or []) == {row['Name'].lstrip('/'), name}, 'network aliases differ')
        require(_mounts(row) == sorted(mount_review[name], key=lambda value: json.dumps(value, sort_keys=True)),
                'reviewed current mount differs')
        for mount in row['Mounts']:
            if mount['Type'] != 'volume':
                continue
            volume_name = mount.get('Name')
            require(isinstance(volume_name, str) and volume_name in volume_inspects, 'volume inspection missing')
            volume = volume_inspects[volume_name]
            require(isinstance(volume, dict) and volume.get('Name') == volume_name, 'volume metadata incomplete')
            require(volume.get('Driver') == 'local' and volume.get('Scope') == 'local', 'unsupported volume driver')
            key = _hash(volume_name)
            observed_volumes[key] = _volume_summary(volume)
        declared = service.get('volumes') or []
        declared_targets = set()
        for mount in declared:
            require(isinstance(mount, dict) and set(mount) <= {'type', 'source', 'target', 'read_only', 'volume'}
                    and (mount.get('volume') is None or mount.get('volume') == {}) and mount.get('type') == 'volume'
                    and isinstance(mount.get('source'), str) and isinstance(mount.get('target'), str),
                    'unsupported declared mount')
            resolved = (compose.get('volumes', {}).get(mount['source']) or {}).get('name', mount['source'])
            matching = [item for item in row['Mounts'] if item['Type'] == 'volume'
                        and item.get('Name') == resolved and item['Destination'] == mount['target']
                        and item['RW'] is (not mount.get('read_only', False))]
            require(len(matching) == 1, 'declared volume differs')
            declared_targets.add(mount['target'])
        image_targets = set((defaults.get('Volumes') or {}).keys())
        require(all(item['Destination'] in declared_targets | image_targets for item in row['Mounts']),
                'unexplained current mount')
        for binding in host.get('Binds') or []:
            require(isinstance(binding, str), 'unsupported host binding')
            matches = []
            for item in row['Mounts']:
                source = item.get('Name') if item['Type'] == 'volume' else item['Source']
                prefix = str(source) + ':' + item['Destination']
                if binding == prefix or binding.startswith(prefix + ':'):
                    mode = binding[len(prefix):].lstrip(':').split(',') if binding != prefix else []
                    if ('ro' in mode) is (not item['RW']):
                        matches.append(item)
            require(len(matches) == 1, 'host binding differs from reviewed mount')
        for mounted in host.get('Mounts') or []:
            require(isinstance(mounted, dict) and {'Type', 'Source', 'Target'} <= set(mounted)
                    and set(mounted) <= {'Type', 'Source', 'Target', 'ReadOnly'},
                    'unsupported Engine mount')
            matches = [item for item in row['Mounts']
                       if item['Type'] == mounted['Type'] and item['Destination'] == mounted['Target']
                       and item['RW'] is (not mounted.get('ReadOnly', False))
                       and (item.get('Name') if mounted['Type'] == 'volume' else item['Source']) == mounted['Source']]
            require(len(matches) == 1, 'Engine mount differs from reviewed current mount')
        extra = [x.replace('=', ':', 1) for x in (service.get('extra_hosts') or [])]
        require(sorted(host.get('ExtraHosts') or []) == sorted(extra), 'extra hosts differ')
        limits = []
        for key, value in (service.get('ulimits') or {}).items():
            if type(value) is int:
                limits.append({'Name': key, 'Soft': value, 'Hard': value})
            else:
                require(isinstance(value, dict) and set(value) == {'soft', 'hard'}, 'unsupported ulimit')
                limits.append({'Name': key, 'Soft': value['soft'], 'Hard': value['hard']})
        require(sorted(host.get('Ulimits') or [], key=lambda item: item['Name'])
                == sorted(limits, key=lambda item: item['Name']), 'ulimits differ')
        tmpfs = {}
        for item in service.get('tmpfs') or []:
            require(isinstance(item, str), 'unsupported tmpfs')
            parts = item.split(':', 1)
            tmpfs[parts[0]] = parts[1] if len(parts) == 2 else ''
        require((host.get('Tmpfs') or {}) == tmpfs, 'tmpfs differs')
        logging = service.get('logging') or {}
        require(host.get('LogConfig') == {'Type': logging.get('driver', ''),
                'Config': {k: str(v) for k, v in (logging.get('options') or {}).items()}}, 'logging differs')
        for key in ('Privileged', 'ReadonlyRootfs', 'PortBindings', 'CapAdd', 'CapDrop', 'SecurityOpt'):
            require(key in host, 'critical host control missing')
        for key in ('CgroupnsMode', 'Cgroup', 'IpcMode', 'PidMode', 'UTSMode', 'UsernsMode', 'NetworkMode',
                    'Devices', 'DeviceRequests', 'DeviceCgroupRules', 'GroupAdd', 'Runtime', 'OomKillDisable',
                    'OomScoreAdj'):
            if key in host:
                require(all(key in rows[x]['HostConfig'] and host[key] == rows[x]['HostConfig'][key]
                            for x in CONTROLS), 'namespace or device control differs from unchanged peers')
        for key in set(host) - MODELED_HOST_KEYS:
            require(all(key in rows[x]['HostConfig'] and host[key] == rows[x]['HostConfig'][key]
                        for x in CONTROLS), 'unsupported host control differs from unchanged peers')
        cpus = Decimal(str(service.get('cpus') or 0)) * 1000000000
        require(cpus == cpus.to_integral_value() and host.get('NanoCpus') == int(cpus)
                and host.get('Memory') == int(service.get('mem_limit') or 0)
                and host.get('MemorySwap') == int(service.get('memswap_limit') or 0), 'resource limits differ')
        require(host.get('PidsLimit') == service.get('pids_limit'), 'pids limit differs')
        shm = service.get('shm_size')
        if shm is not None:
            require((type(shm) is int or (isinstance(shm, str) and shm.isdecimal()))
                    and host.get('ShmSize') == int(shm), 'shared memory limit differs')
        else:
            require(all(host.get('ShmSize') == rows[x]['HostConfig'].get('ShmSize') for x in CONTROLS),
                    'shared memory differs from unchanged peers')
        if name not in CONTROLS:
            require(host.get('RestartPolicy') == {'Name': service.get('restart', 'no'), 'MaximumRetryCount': 0},
                    'restart policy differs')
        def commitment(expected, actual):
            require(expected == actual, 'current model commitment differs')
            return {'expected_sha256': _hash(expected), 'actual_sha256': _hash(actual)}

        desired_cmd = [defaults.get(key) if service.get(source) is None else service[source]
                       for source, key in (('command', 'Cmd'), ('entrypoint', 'Entrypoint'))]
        desired_user = [(defaults.get(key) if service.get(source) is None else service[source]) or ''
                        for source, key in (('user', 'User'), ('working_dir', 'WorkingDir'))]
        desired_security = {'Privileged': bool(service.get('privileged', False)),
                            'ReadonlyRootfs': bool(service.get('read_only', False)),
                            'CapAdd': sorted(service.get('cap_add') or []),
                            'CapDrop': sorted(service.get('cap_drop') or []),
                            'SecurityOpt': sorted(service.get('security_opt') or [])}
        observed_security = {'Privileged': host['Privileged'], 'ReadonlyRootfs': host['ReadonlyRootfs'],
                             'CapAdd': sorted(host['CapAdd'] or []), 'CapDrop': sorted(host['CapDrop'] or []),
                             'SecurityOpt': sorted(host['SecurityOpt'] or [])}
        namespace_keys = sorted(set(host) - MODELED_HOST_KEYS)
        desired_network = {net: {'id': rows['rabbitmq']['NetworkSettings']['Networks'][net].get('NetworkID'),
                                 'aliases': sorted([row['Name'].lstrip('/'), name])} for net in expected_nets}
        actual_network = {net: {'id': detail.get('NetworkID'), 'aliases': sorted(detail.get('Aliases') or [])}
                          for net, detail in networks.items()}
        volume_names = {_hash(mount['Name']) for mount in row['Mounts'] if mount['Type'] == 'volume'}
        desired_volumes = {key: reviewed_volumes[key] for key in volume_names if key in reviewed_volumes}
        actual_volumes = {key: observed_volumes[key] for key in volume_names}
        expected_shm = int(shm) if shm is not None else rows['rabbitmq']['HostConfig'].get('ShmSize')
        desired_resources = [int(cpus), int(service.get('mem_limit') or 0), int(service.get('memswap_limit') or 0),
                             service.get('pids_limit'), expected_shm]
        actual_resources = [host.get('NanoCpus'), host.get('Memory'), host.get('MemorySwap'),
                            host.get('PidsLimit'), host.get('ShmSize')]
        expected_restart = (service.get('restart', 'no') if name not in CONTROLS
                            else external[name]['restart_policy'])
        desired_labels = {**(defaults.get('Labels') or {}), **{k: str(v) for k, v in (service.get('labels') or {}).items()}}
        desired_runtime_config = {'hostname_valid': True, 'domain': service.get('domainname') or '',
                                  'stop_signal': (defaults.get('StopSignal') if stop_signal is None else stop_signal) or '',
                                  'volumes': sorted((defaults.get('Volumes') or {}).keys()),
                                  'streams': [rows['rabbitmq']['Config'].get(key) for key in
                                              ('AttachStderr', 'AttachStdin', 'AttachStdout', 'StdinOnce')]}
        observed_runtime_config = {'hostname_valid': cfg.get('Hostname') == hostname if hostname
                                   else cfg.get('Hostname') in (row['Id'], row['Id'][:12]),
                                   'domain': cfg.get('Domainname') or '', 'stop_signal': cfg.get('StopSignal') or '',
                                   'volumes': sorted((cfg.get('Volumes') or {}).keys()),
                                   'streams': [cfg.get(key) for key in
                                               ('AttachStderr', 'AttachStdin', 'AttachStdout', 'StdinOnce')]}
        commitments[name] = {
            'runtime_config': commitment(desired_runtime_config, observed_runtime_config),
            'labels': commitment(desired_labels, {key: cfg['Labels'].get(key) for key in desired_labels}),
            'image': commitment(image_ids[name], row['Image']),
            'terminal': commitment([bool(service.get('tty', False)), bool(service.get('stdin_open', False))],
                                   [cfg.get('Tty', False), cfg.get('OpenStdin', False)]),
            'dns': commitment([sorted(service.get(k) or []) for k in ('dns', 'dns_search', 'dns_opt')],
                              [sorted(host.get(k) or []) for k in ('Dns', 'DnsSearch', 'DnsOptions')]),
            'network': commitment(desired_network, actual_network),
            'namespace': commitment({key: rows['rabbitmq']['HostConfig'][key] for key in namespace_keys},
                                    {key: host[key] for key in namespace_keys}),
            'resources': commitment(desired_resources, actual_resources),
            'restart': commitment(expected_restart, host['RestartPolicy']['Name']),
            'volumes': commitment(desired_volumes, actual_volumes),
            'environment': commitment(desired, _env(cfg['Env'])),
            'command': commitment(desired_cmd, [cfg.get('Cmd'), cfg.get('Entrypoint')]),
            'execution_identity': commitment(desired_user, [cfg.get('User') or '', cfg.get('WorkingDir') or '']),
            'healthcheck': commitment(desired_health or {}, actual_health or {}),
            'ports': commitment(_ports(service), host.get('PortBindings') or {}),
            'exposed_ports': commitment(sorted(exposed), sorted((cfg.get('ExposedPorts') or {}).keys())),
            'security': commitment(desired_security, observed_security),
            'mounts': commitment(sorted(mount_review[name], key=lambda value: json.dumps(value, sort_keys=True)),
                                 _mounts(row)),
            'extra_hosts': commitment(sorted(extra), sorted(host.get('ExtraHosts') or [])),
            'ulimits': commitment(sorted(limits, key=lambda item: item['Name']),
                                  sorted(host.get('Ulimits') or [], key=lambda item: item['Name'])),
            'tmpfs': commitment(tmpfs, host.get('Tmpfs') or {}),
            'logging': commitment({'Type': logging.get('driver', ''),
                                   'Config': {k: str(v) for k, v in (logging.get('options') or {}).items()}},
                                  host.get('LogConfig')),
        }
        hashes[name] = {'Config.' + k: _hash(v) for k, v in cfg.items()}
        hashes[name].update({'HostConfig.' + k: _hash(v) for k, v in host.items()})
        hashes[name]['Mounts'] = _hash(_mounts(row))
        hashes[name]['NetworkSettings.Networks'] = _hash(networks)
    require(observed_volumes == reviewed_volumes, 'reviewed current volume metadata differs')
    return {'containers': snapshots, 'component_hashes': hashes, 'semantic_commitments': commitments,
            'historical_volume_continuity': 'unknown'}


def capture_current_stack(prior_raw, prior, prior_evidence, output, *, tooling_identity):
    """Capture a *proposed* private model; it is not an approval or authorization."""
    require(json_bytes(prior_raw) == prior, 'prior release bytes differ')
    require(prior.get('schema') == 'cre-runtime-maintenance-v1', 'prior maintenance release required')
    git = Git(prior['programs']['native_git'])
    tooling = identity(tooling_identity)
    tooling_root = local_path(prior['roots']['tooling'])
    git.clean(tooling_root, tooling, tracking=tooling['sha'])
    producer_path = tooling_root / 'scripts/firecrawl-ops/cre_collector/cre_current_stack.py'
    require(Path(__file__).resolve() == producer_path.resolve(), 'current model producer checkout differs')
    producer_raw = read_file(producer_path)
    require(producer_raw == git.command(tooling_root, 'show',
                                       tooling['sha'] + ':scripts/firecrawl-ops/cre_collector/cre_current_stack.py'),
            'current model producer bytes differ from committed tooling')
    validate_prepared_image_history(prior, prior_evidence, git, prior['roots']['development'])
    verify_native_docker(prior)
    version = verify_native_compose(prior)
    root = local_path(prior['roots']['native'])
    files = prior['compose_files']
    require(len(files) == 2 and all(read_file(path) for path in files), 'original Compose files missing')
    overlay = prior['compose_override_path']
    require(read_file(overlay) == prior_evidence.raw('compose_override') == reviewed_override(prior['image_tags']),
            'prior reviewed Compose overlay differs')
    sources = {path: {'sha256': sha(read_file(path)), 'bytes': len(read_file(path))} for path in [*files, overlay]}
    def docker(args):
        return _docker_run(prior, args, cwd=root, check=True, capture_output=True, timeout=120).stdout
    ids = docker(['ps', '--no-trunc', '-q', '--filter', 'label=com.docker.compose.project=firecrawl']).decode().split()
    require(len(ids) == 5 and len(set(ids)) == 5, 'current project fleet differs')
    containers = json_bytes(docker(['inspect', *ids]))
    require(isinstance(containers, list) and len(containers) == 5 and {x.get('Id') for x in containers} == set(ids),
            'current inspection differs')
    by_name = {x['Config']['Labels']['com.docker.compose.service']: x for x in containers}
    require(set(by_name) == SERVICES, 'current services differ')
    old_before = unique(prior_evidence.obj('container_config_before'), 'service')
    images, volume_inspects = {}, {}
    for name, container in by_name.items():
        expected_image = prior['expected_images'].get(name, old_before[name]['image'])
        require(container['Image'] == expected_image, 'current image differs from reviewed prior image')
        image = json_bytes(docker(['image', 'inspect', expected_image]))
        require(isinstance(image, list) and len(image) == 1 and image[0].get('Id') == expected_image,
                'current image inspection differs')
        images[name] = image[0]
        if name in prior['expected_images']:
            require(image[0].get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision')
                    == prior['new']['sha'], 'current image source revision differs')
        for mount in container.get('Mounts', []):
            if mount.get('Type') == 'volume':
                volume_name = mount.get('Name')
                require(isinstance(volume_name, str) and volume_name, 'volume name missing')
                volume = json_bytes(docker(['volume', 'inspect', volume_name]))
                require(isinstance(volume, list) and len(volume) == 1 and volume[0].get('Name') == volume_name,
                        'current volume inspection differs')
                volume_inspects[volume_name] = volume[0]
    compose_cmd = ['compose', '--project-directory', prior['roots']['native'], '--project-name', 'firecrawl']
    for path in [*files, overlay]:
        compose_cmd += ['-f', path]
    require(verify_native_compose(prior) == version, 'Compose executable changed')
    compose = json_bytes(docker(compose_cmd + ['config', '--format', 'json']))
    require(verify_native_compose(prior) == version, 'Compose executable changed')
    old_runtime = unique(prior_evidence.obj('runtime_snapshot')['containers'], 'id')
    prior_compatibility = {}
    for name, row in by_name.items():
        old_row = old_before[name]
        require(old_row['id'] in old_runtime and old_runtime[old_row['id']]['name'] == row['Name']
                and old_runtime[old_row['id']]['image'] == old_row['image'], 'prior runtime identity differs')
        old_image = json_bytes(docker(['image', 'inspect', old_row['image']]))
        require(isinstance(old_image, list) and len(old_image) == 1
                and old_image[0].get('Id') == old_row['image'], 'prior image inspection differs')
        allowed = prior['allowed_image_default_changes'].get(name, [])
        explicit = set((compose['services'][name].get('environment') or {}).keys())
        prior_compatibility[name] = verify_prior_environment_and_caps(
            old_runtime[old_row['id']], row, old_image[0], images[name], explicit, allowed)
    snapshots = [snapshot_container(row) for row in containers]
    model = {'expected_images': {name: row['Image'] for name, row in by_name.items()},
             'external_before': {name: old_before[name] for name in CONTROLS},
             'reviewed_containers': sorted(snapshots, key=lambda x: x['service']),
             'reviewed_mounts': {name: _mounts(row) for name, row in by_name.items()},
             'reviewed_volumes': {_hash(name): _volume_summary(value) for name, value in volume_inspects.items()}}
    qualified = qualify_current_stack(containers, compose, images, model, volume_inspects)
    reread = json_bytes(docker(['inspect', *ids]))
    require(sorted((snapshot_container(x) for x in reread), key=lambda x: x['service'])
            == model['reviewed_containers'], 'current fleet changed during observation')
    require(all(sources[path] == {'sha256': sha(read_file(path)), 'bytes': len(read_file(path))}
                for path in sources), 'Compose source changed during observation')
    require(verify_native_compose(prior) == version, 'Compose executable changed')
    verify_native_docker(prior)
    prior_evidence.recheck()
    git.clean(tooling_root, tooling, tracking=tooling['sha'])
    require(read_file(producer_path) == producer_raw, 'current model producer changed during observation')
    result = {'schema': 'cre-current-stack-model-v1', 'prior_release_sha256': sha(prior_raw),
              'source_identity': prior['new'], 'historical_volume_continuity': 'unknown',
              'tooling_identity': tooling, 'producer_sha256': sha(producer_raw),
              'model': model, 'component_hashes': qualified['component_hashes'],
              'semantic_commitments': qualified['semantic_commitments'], 'compose_sources': sources,
              'prior_compatibility': prior_compatibility,
              'dependency_metadata': {name: _dependency_metadata(compose['services'][name],
                                      by_name[name]['Config']['Labels']) for name in sorted(SERVICES)},
              'native_docker_sha256': prior['native_docker_sha256'], 'native_compose': prior['native_compose'],
              'compose_version': version, 'prepared_images_sha256': sha(prior_evidence.raw('prepared_images'))}
    output = Path(output)
    require(output.is_absolute() and not output.exists() and not str(output).startswith('/mnt/')
            and all(not p.is_symlink() for p in output.parents), 'new private ext4 output required')
    output.mkdir(mode=0o700)
    raw = (json.dumps(result, sort_keys=True, indent=2) + '\n').encode()
    fd = os.open(output / 'current-stack-model.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'path': str(output / 'current-stack-model.json'), 'sha256': sha(raw), 'bytes': len(raw)}


def resolve_image_source(packet, evidence, git, repo):
    """Follow bounded, authenticated no-image releases to the image producer."""
    from cre_runtime_maintenance_continuation import validate_continuation_code

    current, inputs, seen = packet, evidence, set()
    for _ in range(8):
        continuation = current.get('continuation')
        require(isinstance(continuation, dict) and isinstance(continuation.get('failed_release'), str),
                'image predecessor missing')
        role = continuation['failed_release']
        raw = inputs.raw(role)
        digest = sha(raw)
        require(digest not in seen, 'image predecessor cycle')
        seen.add(digest)
        prior = json_bytes(raw)
        require(prior.get('schema') == 'cre-runtime-maintenance-v1', 'image predecessor schema differs')
        previous = Evidence(prior, evidence.read)
        if prior.get('expected_images'):
            inputs.recheck()
            evidence.original_payloads.update(inputs.original_payloads)
            evidence.original_payloads.update({inputs.inputs[key]['path']: value for key, value in inputs.original.items()})
            return raw, prior, previous
        require(prior.get('expected_images') == {} and prior.get('image_tags') == {}
                and prior.get('install_scope') == [], 'image predecessor has unreviewed scope')
        validate_continuation_code(prior, git, repo)
        inputs.recheck()
        evidence.original_payloads.update(inputs.original_payloads)
        evidence.original_payloads.update({inputs.inputs[key]['path']: value for key, value in inputs.original.items()})
        current, inputs = prior, previous
    require(False, 'image predecessor depth exceeded')


def validate_current_stack(packet, evidence, receipt, *, git=None):
    """Validate retained commitments and an independent review, never live state.

    Hash equality is evidence of equality to the approved commitment; it does
    not reconstruct private environment or mount-source values from hashes.
    """
    from cre_code_handoff_evidence import canonical_path

    continuation = packet['continuation']
    role = continuation['current_stack']
    model_raw = evidence.raw(role)
    require(json_bytes(model_raw) == receipt and receipt.get('schema') == 'cre-current-stack-model-v1',
            'current model bytes or schema differ')
    git = git if git is not None else Git(packet['programs']['native_git'])
    prior_raw, prior, previous = resolve_image_source(packet, evidence, git, packet['roots']['development'])
    require(receipt.get('prior_release_sha256') == sha(prior_raw)
            and receipt.get('source_identity') == prior.get('new')
            and receipt.get('historical_volume_continuity') == 'unknown', 'current model provenance differs')
    model = receipt.get('model')
    require(isinstance(model, dict) and set(model) == {'expected_images', 'external_before', 'reviewed_containers',
                                                       'reviewed_mounts', 'reviewed_volumes'}, 'current model malformed')
    snapshots = unique(model['reviewed_containers'], 'service')
    before = unique(evidence.obj('container_config_before'), 'service')
    require(set(snapshots) == SERVICES and before == snapshots, 'current snapshot differs from reviewed baseline')
    require(all(row.get('status') == 'running' and isinstance(row.get('started_at'), str)
                and isinstance(row.get('id'), str) and row['id'] and row.get('image') == model['expected_images'].get(name)
                for name, row in snapshots.items()) and set(model['expected_images']) == SERVICES,
            'current fleet identity incomplete')
    require(all(model['expected_images'].get(name) == image for name, image in prior['expected_images'].items()),
            'current selected images differ from prior build')
    old = unique(previous.obj('container_config_before'), 'service')
    require(model['external_before'] == {name: old[name] for name in CONTROLS}
            and all(snapshots[name] == old[name] for name in CONTROLS), 'external services changed')
    prior_runtime = unique(previous.obj('runtime_snapshot')['containers'], 'id')
    compatibility = receipt.get('prior_compatibility')
    require(isinstance(compatibility, dict) and set(compatibility) == SERVICES,
            'prior environment and resource commitments missing')
    for name in SERVICES:
        require(old[name]['id'] in prior_runtime, 'prior runtime container missing')
        prior_row = prior_runtime[old[name]['id']]
        expected_caps = [prior_row.get(key) for key in ('memory', 'nano_cpus', 'cpu_quota', 'cpu_period')]
        entry = compatibility[name]
        require(isinstance(entry, dict) and set(entry) == {'environment', 'resources'}
                and all(isinstance(entry[key], dict)
                        and set(entry[key]) == {'expected_sha256', 'actual_sha256'}
                        and entry[key]['expected_sha256'] == entry[key]['actual_sha256']
                        for key in entry)
                and entry['environment']['expected_sha256'] == prior_row.get('environment_sha256')
                and all(type(value) is int for value in expected_caps)
                and entry['resources']['expected_sha256'] == _hash(expected_caps),
                'prior environment or resource commitment differs')
    validate_prepared_image_history(prior, previous, git, packet['roots']['development'])
    require(receipt.get('prepared_images_sha256') == sha(previous.raw('prepared_images')),
            'current image build evidence differs')
    for service in ('api', 'playwright-service-ts', 'nuq-postgres'):
        path = 'apps/' + service
        require(git.text(packet['roots']['development'], 'rev-parse', prior['new']['sha'] + ':' + path)
                == git.text(packet['roots']['development'], 'rev-parse', packet['new']['sha'] + ':' + path),
                'selected image build context changed')
    require(previous.raw('compose_override') == reviewed_override(prior['image_tags']), 'prior overlay differs')
    sources = receipt.get('compose_sources')
    source_proof = previous.obj('compose_sources')
    require(isinstance(sources, dict) and set(sources) == set(prior['compose_files'] + [prior['compose_override_path']]),
            'current Compose source scope differs')
    for path in prior['compose_files']:
        require(sources[path] == source_proof.get(path), 'original Compose source differs')
    overlay_raw = previous.raw('compose_override')
    require(sources[prior['compose_override_path']] == {'sha256': sha(overlay_raw), 'bytes': len(overlay_raw)},
            'current overlay source differs')
    require(receipt.get('native_docker_sha256') == prior['native_docker_sha256']
            and receipt.get('native_compose') == prior['native_compose'], 'current native tools differ')
    tooling = identity(packet['tooling'])
    producer = git.command(packet['roots']['development'], 'show',
                           tooling['sha'] + ':scripts/firecrawl-ops/cre_collector/cre_current_stack.py')
    require(receipt.get('tooling_identity') == tooling and receipt.get('producer_sha256') == sha(producer),
            'current model producer identity differs')
    hashes = receipt.get('component_hashes')
    require(isinstance(hashes, dict) and set(hashes) == SERVICES
            and all(isinstance(fields, dict) and fields and all(isinstance(k, str) and isinstance(v, str)
                    and re.fullmatch(r'[0-9a-f]{64}', v) for k, v in fields.items()) for fields in hashes.values()),
            'current component commitments malformed')
    commitments = receipt.get('semantic_commitments')
    require(isinstance(commitments, dict) and set(commitments) == SERVICES
            and all(isinstance(fields, dict) and set(fields) == COMMITMENT_FIELDS
                    and all(isinstance(pair, dict) and set(pair) == {'expected_sha256', 'actual_sha256'}
                            and isinstance(pair['expected_sha256'], str)
                            and re.fullmatch(r'[0-9a-f]{64}', pair['expected_sha256'])
                            and pair['expected_sha256'] == pair['actual_sha256'] for pair in fields.values())
                    for fields in commitments.values()), 'current semantic commitments incomplete or differ')
    dependencies = receipt.get('dependency_metadata')
    require(isinstance(dependencies, dict) and set(dependencies) == SERVICES
            and all(isinstance(value, dict) and set(value) == {'desired_sha256', 'actual_sha256',
                    'desired_count', 'actual_count', 'matches'} and type(value['desired_count']) is int
                    and type(value['actual_count']) is int and value['desired_count'] >= 0
                    and value['actual_count'] >= 0 and type(value['matches']) is bool
                    and all(isinstance(value[k], str) and re.fullmatch(r'[0-9a-f]{64}', value[k])
                            for k in ('desired_sha256', 'actual_sha256'))
                    and value['matches'] is (value['desired_sha256'] == value['actual_sha256'])
                    for value in dependencies.values()), 'dependency metadata incomplete')
    review = evidence.obj(continuation['current_stack_review'])
    body = evidence.raw('current_stack_review_body')
    validate_review(review, body, {'current_stack_sha256': sha(model_raw), 'prior_release_sha256': sha(prior_raw)},
                    'current-stack-baseline')
    require(review.get('historical_volume_continuity') == 'unknown'
            and review.get('dependency_metadata_reviewed') is True
            and canonical_path(review.get('body', {}).get('path'))
            == canonical_path(evidence.inputs['current_stack_review_body']['path']),
            'review must explicitly retain historical volume uncertainty')
    previous.recheck()
    for role, raw in previous.original.items():
        evidence.original_payloads[previous.inputs[role]['path']] = raw
    evidence.original_payloads.update(previous.original_payloads)
    return receipt


def validate_current_stack_live(packet, evidence, receipt, output):
    """Require a fresh independent observation to equal the reviewed model."""
    validate_current_stack(packet, evidence, receipt)
    git = Git(packet['programs']['native_git'])
    prior_raw, prior, prior_evidence = resolve_image_source(packet, evidence, git, packet['roots']['development'])
    fresh_ref = capture_current_stack(prior_raw, prior, prior_evidence, output, tooling_identity=packet['tooling'])
    require(json_bytes(read_file(fresh_ref['path'])) == receipt, 'fresh current stack differs from review')
    evidence.recheck()
    return receipt


def main(argv=None):
    """Preview by default; explicit observe performs read-only Docker inspection."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior-release', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--observe', action='store_true')
    parser.add_argument('--tooling-sha')
    parser.add_argument('--tooling-tree')
    args = parser.parse_args(argv)
    raw = read_file(args.prior_release)
    prior = json_bytes(raw)
    require(prior.get('schema') == 'cre-runtime-maintenance-v1', 'prior maintenance release required')
    if not args.observe:
        print(json.dumps({'mode': 'preview', 'prior_release_sha256': sha(raw), 'output': args.output}))
        return
    require(args.tooling_sha and args.tooling_tree, 'explicit committed tooling identity required')
    evidence = Evidence(prior, read_file)
    result = capture_current_stack(raw, prior, evidence, args.output,
                                   tooling_identity={'sha': args.tooling_sha, 'tree': args.tooling_tree})
    print(json.dumps({'mode': 'observed', 'model': result}))


if __name__ == '__main__':
    main()
