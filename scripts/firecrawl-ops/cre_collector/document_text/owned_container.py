"""Private container lifecycle used by document adapters; no scheduling or retries."""
import re
import subprocess

import ocr


def run(arguments, name, nonce, extraction, settled, removed_check, read_results, timeout):
    container = None; verified_id = None; identity_confirmed = False
    try:
        created = subprocess.run(arguments, capture_output=True, timeout=30, check=False)
        candidate = created.stdout.decode('ascii', errors='ignore').strip()
        container = ocr.owned_id(name, nonce)
        if created.returncode or not re.fullmatch(r'[a-f0-9]{64}', candidate) or container != candidate:
            extraction['error_code'] = 'container_identity_unconfirmed'
            extraction['warnings'].append('container_cleanup_unconfirmed')
        else:
            identity_confirmed = True
            try:
                process = subprocess.run(['docker', 'start', '--attach', container], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=timeout, check=False)
                extraction['converter_exit_code'] = process.returncode
                if process.returncode:
                    extraction['warnings'].append('worker_failed_partial_output')
            except subprocess.TimeoutExpired:
                extraction['warnings'].append('worker_timeout_partial_output')
            # Stop before reading: a timed-out daemon-side process may still write.
            stopped = subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=30, check=False)
            if stopped.returncode or not removed_check(container):
                extraction['warnings'].append('container_cleanup_unconfirmed')
            else:
                verified_id = container
                container = None
                settled(verified_id)
                read_results()
    except (OSError, subprocess.SubprocessError):
        extraction['error_code'] = 'container_operation_failed'
    finally:
        if container is None and verified_id is None:
            try:
                container = ocr.owned_id(name, nonce)
            except (OSError, subprocess.SubprocessError):
                extraction['warnings'].append('container_cleanup_unconfirmed')
        if not container and extraction.get('error_code') in ('container_identity_unconfirmed', 'container_operation_failed'):
            extraction['warnings'].append('container_cleanup_unconfirmed')
        if container:
            try:
                removed = subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=30, check=False)
                if removed.returncode or not removed_check(container):
                    extraction['warnings'].append('container_cleanup_unconfirmed')
                elif identity_confirmed:
                    settled(container)
            except (OSError, subprocess.SubprocessError):
                extraction['warnings'].append('container_cleanup_unconfirmed')
