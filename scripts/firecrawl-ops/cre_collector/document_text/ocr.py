"""Explicit local PDF retry of a retained AnyDoc artifact."""
try:
    from document_text.poppler_modes import (
        LAYOUT,
        MODES,
        TEXT,
        config,
        mode_from_config,
    )
except ModuleNotFoundError:  # Direct script invocation from document_text.
    from poppler_modes import LAYOUT, MODES, TEXT, config, mode_from_config
import argparse
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buildout_observation_artifact import invalid_number, unique_object
from buildout_observation_io import read_consistent
from source_evidence.events import http_url, timestamp

IMAGE = 'quay.io/docling-project/docling-serve-cpu@sha256:528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0'
MAX_INPUT = 100 * 1024 * 1024
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
TIMEOUT = 120


def digest(value):
    return hashlib.sha256(value).hexdigest()


def placeholder_only(text):
    """Recognize Docling's image-only Markdown without discarding real text."""
    return re.fullmatch(r'\s*<!-- image -->(?:\s*<!-- image -->)*\s*', text) is not None


def read_limited(path, limit):
    data = read_consistent(path, limit)
    if len(data) > limit:
        raise ValueError('input exceeds limit')
    return data


def docker_arguments(input_path, output_path, name, nonce):
    return ['docker', 'create', '--name', name, '--label', f'cre.ocr.owner={nonce}', '--pull=never', '--network=none', '--read-only', '--cpus=1', '--memory=3g',
            '--pids-limit=128', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--ulimit', 'fsize=8388608:8388608',
            '--tmpfs=/tmp:rw,nosuid,size=512m',
            '--user', f'{os.getuid()}:{os.getgid()}', '--env', 'USER=docling', '--env', 'HF_HUB_OFFLINE=1',
            '--env', 'TRANSFORMERS_OFFLINE=1', '--env', 'HF_HOME=/opt/app-root/src/.cache/huggingface',
            '--mount', f'type=bind,src={input_path},dst=/input.pdf,readonly',
            '--mount', f'type=bind,src={output_path},dst=/output', '--entrypoint', 'docling', IMAGE,
            '--ocr', '--ocr-engine', 'tesseract', '--no-enable-remote-services', '--abort-on-error', '--to', 'md',
            '--image-export-mode', 'placeholder', '--document-timeout', '90', '--num-threads', '1', '--device', 'cpu',
            '--artifacts-path', '/opt/app-root/src/.cache/docling/models', '--output', '/output', '/input.pdf']


def make_attempt(first, first_raw, raw, engine='docling', poppler_version=None, *, relationship='retry', poppler_mode=TEXT):
    if json.loads(first_raw, object_pairs_hook=unique_object, parse_constant=invalid_number) != first:
        raise ValueError('attempt bytes disagree with decoded artifact')
    extraction = first.get('extraction', {})
    eligible = extraction.get('status') == 'ocr_needed' or (
        extraction.get('status') == 'failed' and extraction.get('error_code') == 'unsupported'
    )
    if relationship == 'complement' and engine == 'poppler':
        if extraction.get('status') == 'extracted':
            text = extraction.get('text')
            if (not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_TEXT
                    or extraction.get('text_sha256') != digest(text.encode('utf-8'))
                    or extraction.get('coverage') != 'unknown'):
                raise ValueError('invalid extracted AnyDoc text')
            eligible = True
    elif relationship != 'retry':
        raise ValueError('unsupported parser relationship')
    if first.get('schema_version') != 'cre-document-text/v1' or not eligible:
        raise ValueError('an OCR-needed or unsupported AnyDoc PDF artifact is required')
    if poppler_mode not in MODES or (poppler_mode != TEXT and (engine != 'poppler' or relationship != 'complement')):
        raise ValueError('invalid Poppler mode')
    if engine not in ('docling', 'poppler') or (engine == 'poppler' and not poppler_version):
        raise ValueError('a supported engine and version are required')
    if first.get('parser', {}).get('name') != '@firecrawl/anydoc':
        raise ValueError('expected AnyDoc attempt')
    source = first.get('source', {})
    if http_url(source.get('url')) is None:
        raise ValueError('source URL required')
    timestamp(source.get('observed_at'))
    if source.get('format') != 'pdf' or source.get('sha256') != digest(raw) or source.get('byte_length') != len(raw):
        raise ValueError('original file does not match the retained PDF')
    parser = {'name': 'docling', 'version': '2.95.0',
              'config': {'image': IMAGE, 'ocr_engine': 'tesseract', 'network': 'none',
                         'cpu': 1, 'memory_bytes': 3 * 1024 ** 3, 'timeout_seconds': TIMEOUT,
                         'max_output_file_bytes': 8 * 1024 ** 2, 'max_text_bytes': MAX_TEXT}}
    if engine == 'poppler':
        parser = {'name': 'poppler-pdftotext', 'version': poppler_version,
                  'config': config(poppler_mode)}
    result = {'schema_version': 'cre-document-text/v1', 'source': dict(source),
            'parser': parser,
            'extraction': {'status': 'failed', 'coverage': 'unknown', 'page_count': first['extraction'].get('page_count'),
                           'ocr_needed_pages': first['extraction'].get('ocr_needed_pages', []),
                           'text_format': 'plain' if engine == 'poppler' else 'markdown',
                           'text': None, 'text_sha256': None, 'error_code': None, 'warnings': [],
                           'previous_attempt_sha256': digest(first_raw)}}
    if relationship == 'complement':
        result['extraction']['relationship'] = 'complement_of'
    return result


def _limit_log():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_LOG, MAX_LOG))


def owned_id(name, nonce):
    result = subprocess.run(['docker', 'inspect', '--format', '{{.Id}} {{index .Config.Labels "cre.ocr.owner"}}', name],
                            capture_output=True, timeout=15, check=False)
    fields = result.stdout.decode('ascii', errors='ignore').strip().split()
    return fields[0] if result.returncode == 0 and len(fields) == 2 and fields[1] == nonce and re.fullmatch(r'[a-f0-9]{64}', fields[0]) else None


def convert(raw, attempt):
    with tempfile.TemporaryDirectory(prefix='cre-docling-') as folder:
        path = Path(folder); path.chmod(0o700)
        input_path = path / 'input.pdf'; input_path.write_bytes(raw); input_path.chmod(0o600)
        output_dir = path / 'output'; output_dir.mkdir(mode=0o700)
        nonce = uuid.uuid4().hex
        name = 'cre-docling-' + nonce
        container = None
        try:
            try:
                created = subprocess.run(docker_arguments(input_path, output_dir, name, nonce), capture_output=True, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                # Creation may have completed even when the client timed out.
                try:
                    container = owned_id(name, nonce)
                    if not container:
                        attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')
                except (OSError, subprocess.SubprocessError):
                    attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')
                attempt['extraction']['error_code'] = 'container_create_unknown'
                return attempt
            candidate = created.stdout.decode('ascii', errors='ignore').strip()
            if created.returncode or not re.fullmatch(r'[a-f0-9]{64}', candidate):
                container = owned_id(name, nonce)
                if not container:
                    attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')
                attempt['extraction']['error_code'] = 'container_create_failed'
                return attempt
            container = owned_id(name, nonce)
            if container != candidate:
                attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')
                attempt['extraction']['error_code'] = 'container_identity_unconfirmed'
                return attempt
            with tempfile.TemporaryFile() as log:
                try:
                    run = subprocess.run(['docker', 'start', '--attach', container], stdout=log, stderr=log,
                                         timeout=TIMEOUT, check=False, preexec_fn=_limit_log)
                except subprocess.TimeoutExpired:
                    attempt['extraction']['error_code'] = 'timeout'
                    return attempt
                log.seek(0); log_data = log.read(MAX_LOG + 1)
            if len(log_data) > MAX_LOG:
                attempt['extraction']['error_code'] = 'log_too_large'
                return attempt
            if b'OSD failed' in log_data:
                attempt['extraction']['warnings'].append('tesseract_osd_failed')
            if b'ERROR' in log_data:
                attempt['extraction']['warnings'].append('docling_logged_error')
            attempt['extraction']['converter_exit_code'] = run.returncode
            if run.returncode:
                attempt['extraction']['warnings'].append('converter_failed_partial_output')
            output = output_dir / 'input.md'
            if sorted(p.name for p in output_dir.iterdir()) != ['input.md']:
                attempt['extraction']['error_code'] = 'converter_failed' if run.returncode else 'output_unavailable'
                return attempt
            try:
                data = read_limited(output, MAX_TEXT)
                text = data.decode('utf-8')
            except (OSError, ValueError, UnicodeError):
                attempt['extraction']['error_code'] = 'invalid_or_oversized_output'
                return attempt
            if not text.strip():
                attempt['extraction']['error_code'] = 'empty_output'
                return attempt
            if placeholder_only(text):
                attempt['extraction']['error_code'] = 'placeholder_only_output'
                return attempt
            attempt['extraction'].update(status='extracted', text=text, text_sha256=digest(data))
            return attempt
        except (OSError, subprocess.SubprocessError):
            if not container:
                attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')
            attempt['extraction']['error_code'] = 'container_operation_failed'
            return attempt
        finally:
            if container:
                try:
                    removed = subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, timeout=30, check=False)
                    if removed.returncode:
                        attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')
                except (OSError, subprocess.SubprocessError):
                    attempt['extraction']['warnings'].append('container_cleanup_unconfirmed')


def poppler_version(executable='pdftotext'):
    result = subprocess.run([executable, '-v'], capture_output=True, timeout=10, check=False)
    match = re.search(rb'pdftotext version ([0-9]+(?:\.[0-9]+)+)', result.stderr[:4096])
    if result.returncode or not match:
        raise ValueError('cannot identify local pdftotext version')
    return match.group(1).decode('ascii')


def convert_poppler(raw, attempt, executable='pdftotext', *, mode=TEXT):
    parser = attempt.get('parser', {})
    if (parser.get('name') != 'poppler-pdftotext'
            or mode not in MODES or mode_from_config(parser.get('config')) != mode):
        raise ValueError('Poppler mode disagrees with attempt')
    # Local extraction only: Poppler cannot OCR image-only pages. Never retain
    # its unbounded diagnostic text, which may contain document information.
    with tempfile.TemporaryDirectory(prefix='cre-poppler-') as folder:
        path = Path(folder); path.chmod(0o700)
        source = path / 'input.pdf'; source.write_bytes(raw); source.chmod(0o600)
        output = path / 'output.txt'
        try:
            with tempfile.TemporaryFile() as log:
                run = subprocess.run([executable, *(['-layout'] if mode == LAYOUT else []), '-enc', 'UTF-8', str(source), str(output)],
                                     stdout=log, stderr=log, timeout=TIMEOUT, check=False,
                                     preexec_fn=_limit_log)
                if log.tell():
                    attempt['extraction']['warnings'].append('poppler_logged_diagnostic')
            attempt['extraction']['converter_exit_code'] = run.returncode
            if run.returncode:
                attempt['extraction']['warnings'].append('converter_failed_partial_output')
            if not output.exists():
                attempt['extraction']['error_code'] = 'converter_failed' if run.returncode else 'output_unavailable'
                return attempt
            data = read_limited(output, MAX_TEXT)
            text = data.decode('utf-8')
            if not text.strip():
                attempt['extraction']['error_code'] = 'empty_output'
                return attempt
            attempt['extraction'].update(status='extracted', text=text, text_sha256=digest(data))
        except subprocess.TimeoutExpired:
            attempt['extraction']['error_code'] = 'timeout'
        except (OSError, ValueError, UnicodeError):
            attempt['extraction']['error_code'] = 'invalid_or_oversized_output'
        return attempt


def write_private(path, artifact):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(artifact, out, ensure_ascii=False, allow_nan=False)
        out.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True); parser.add_argument('--anydoc-attempt', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--engine', choices=('docling', 'poppler'), default='docling',
                        help='docling OCR (default) or local Poppler text extraction without OCR')
    parser.add_argument('--relationship', choices=('retry', 'complement'), default='retry',
                        help='complement also permits extracted AnyDoc PDFs; Poppler only')
    parser.add_argument('--poppler-path', default='pdftotext')
    parser.add_argument('--poppler-mode', choices=MODES, default=TEXT)
    args = parser.parse_args()
    raw = read_limited(args.input, MAX_INPUT)
    first_raw = read_limited(args.anydoc_attempt, MAX_ARTIFACT)
    first = json.loads(first_raw, object_pairs_hook=unique_object, parse_constant=invalid_number)
    version = poppler_version(args.poppler_path) if args.engine == 'poppler' else None
    attempt = make_attempt(first, first_raw, raw, args.engine, version, relationship=args.relationship, poppler_mode=args.poppler_mode)
    if args.relationship == 'complement':
        attempt['parser']['config']['executable'] = args.poppler_path
    result = convert_poppler(raw, attempt, args.poppler_path, mode=args.poppler_mode) if args.engine == 'poppler' else convert(raw, attempt)
    write_private(args.out, result)
    print('OCR attempt recorded:', result['extraction']['status'])
    return 0 if result['extraction']['status'] == 'extracted' else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError):
        print('OCR attempt refused or failed; no document content printed.', file=sys.stderr)
        sys.exit(1)
