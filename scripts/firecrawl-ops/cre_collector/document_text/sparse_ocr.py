"""Bounded, offline sparse OCR complement for a retained local PDF."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocr

TIMEOUT = 210
MAX_TEXT = ocr.MAX_TEXT
MAX_LAYOUT = 1024 * 1024
MAX_METADATA = 2 * 1024 * 1024
WORKER = Path(__file__).with_name('sparse_ocr_worker.py').resolve()


def _arguments(source, output, name, nonce, max_pages, worker=WORKER):
    return ['docker', 'create', '--name', name, '--label', f'cre.ocr.owner={nonce}',
            '--pull=never', '--network=none', '--read-only', '--cpus=1', '--memory=3g',
            '--pids-limit=128', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--ulimit', 'fsize=134217728:134217728', '--tmpfs=/tmp:rw,nosuid,size=512m',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '--mount', f'type=bind,src={source},dst=/input.pdf,readonly',
            '--mount', f'type=bind,src={worker},dst=/worker.py,readonly',
            '--mount', f'type=bind,src={output},dst=/output', '--entrypoint', 'python',
            ocr.IMAGE, '/worker.py', str(max_pages)]


def _write_receipt(directory, name, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return ocr.digest(raw)


def run(pdf_path, prior_artifacts, output_dir, *, max_pages, trigger_policy):
    """Write a private artifact. Prior artifacts are ordered AnyDoc then Poppler paths."""
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise ValueError('max_pages must be between 1 and 1000')
    if trigger_policy not in ('no_text', 'all_pages'):
        raise ValueError('invalid trigger policy')
    if len(prior_artifacts) != 2:
        raise ValueError('AnyDoc and Poppler artifacts required')
    raw = ocr.read_limited(pdf_path, ocr.MAX_INPUT)
    prior_raw = [ocr.read_limited(p, ocr.MAX_ARTIFACT) for p in prior_artifacts]
    prior = [json.loads(p, object_pairs_hook=ocr.unique_object, parse_constant=ocr.invalid_number) for p in prior_raw]
    first, second = prior
    if trigger_policy == 'no_text' and any(
            isinstance(item.get('extraction', {}).get('text'), str)
            and item['extraction']['text'].strip() for item in prior):
        raise ValueError('no_text policy requires both prior attempts to have no text')
    # Reuse the existing source and AnyDoc validation, including extracted complements.
    attempt = ocr.make_attempt(first, prior_raw[0], raw, 'poppler', 'validated', relationship='complement')
    if (second.get('schema_version') != 'cre-document-text/v1' or second.get('source') != first.get('source')
            or second.get('parser', {}).get('name') != 'poppler-pdftotext'
            or second.get('extraction', {}).get('previous_attempt_sha256') != ocr.digest(prior_raw[0])):
        raise ValueError('Poppler artifact does not bind the AnyDoc attempt and source')
    second_extraction = second['extraction']
    if second_extraction.get('coverage') != 'unknown':
        raise ValueError('invalid Poppler coverage')
    if second_extraction.get('status') == 'extracted':
        text = second_extraction.get('text')
        if (not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_TEXT
                or second_extraction.get('text_sha256') != ocr.digest(text.encode('utf-8'))):
            raise ValueError('invalid Poppler text')
    worker_bytes = WORKER.read_bytes()
    attempt['parser'] = {'name': 'pdfium-tesseract-sparse', 'version': '1', 'config': {
        'image': ocr.IMAGE, 'pypdfium2': '5.8.0', 'pdfium': '149.0.7825.0',
        'tesseract': '4.1.1', 'language': 'eng', 'psm': 11, 'osd': False,
        'dpi': 200, 'max_pixels_per_page': 25_000_000, 'max_pages': max_pages,
        'per_page_timeout_seconds': 60, 'timeout_seconds': TIMEOUT, 'max_text_bytes': MAX_TEXT, 'max_layout_bytes': MAX_LAYOUT,
        'network': 'none', 'cpu': 1, 'memory_bytes': 3 * 1024**3, 'trigger_policy': trigger_policy,
        'worker_sha256': ocr.digest(worker_bytes)}}
    extraction = attempt['extraction']
    extraction.update(previous_attempt_sha256=ocr.digest(prior_raw[1]), relationship='complement_of',
                      text_format='plain', page_count=None, pages=[], omitted_pages=None, layout_omitted_pages=None,
                      warnings=['coverage_unknown'])
    out = Path(output_dir)
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    parent_fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    nonce = uuid.uuid4().hex; name = 'cre-sparse-' + nonce
    config_bytes = json.dumps(attempt['parser']['config'], ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode('utf-8')
    intent_sha = _write_receipt(out, 'owner-intent.json', {
        'schema_version': 'cre-sparse-ocr-owner/v1', 'name': name, 'nonce': nonce,
        'source_sha256': ocr.digest(raw), 'prior_attempt_sha256': [ocr.digest(value) for value in prior_raw],
        'worker_sha256': ocr.digest(worker_bytes), 'config_sha256': ocr.digest(config_bytes), 'uid': os.getuid()})

    def settled(container_id):
        _write_receipt(out, 'owner-settled.json', {'schema_version': 'cre-sparse-ocr-settlement/v1',
                       'intent_sha256': intent_sha, 'container_id': container_id, 'state': 'removed_verified'})

    with tempfile.TemporaryDirectory(prefix='cre-sparse-ocr-') as temp:
        root = Path(temp); root.chmod(0o700)
        source = root / 'input.pdf'; source.write_bytes(raw); source.chmod(0o600)
        results = root / 'output'; results.mkdir(mode=0o700)
        worker = root / 'worker.py'; worker.write_bytes(worker_bytes); worker.chmod(0o600)
        container = None; verified_id = None; identity_confirmed = False
        try:
            created = subprocess.run(_arguments(source, results, name, nonce, max_pages, worker), capture_output=True, timeout=30, check=False)
            candidate = created.stdout.decode('ascii', errors='ignore').strip()
            container = ocr.owned_id(name, nonce)
            if created.returncode or not re.fullmatch(r'[a-f0-9]{64}', candidate) or container != candidate:
                extraction['error_code'] = 'container_identity_unconfirmed'
                extraction['warnings'].append('container_cleanup_unconfirmed')
            else:
                identity_confirmed = True
                try:
                    process = subprocess.run(['docker', 'start', '--attach', container], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
                    extraction['converter_exit_code'] = process.returncode
                    if process.returncode:
                        extraction['warnings'].append('worker_failed_partial_output')
                except subprocess.TimeoutExpired:
                    extraction['warnings'].append('worker_timeout_partial_output')
                # Stop before reading: a timed-out daemon-side process may still write.
                stopped = subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=30, check=False)
                if stopped.returncode or not _removed(container):
                    extraction['warnings'].append('container_cleanup_unconfirmed')
                else:
                    verified_id = container
                    container = None
                    settled(verified_id)
                    _read_results(results, extraction, max_pages)
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
                    if removed.returncode or not _removed(container):
                        extraction['warnings'].append('container_cleanup_unconfirmed')
                    elif identity_confirmed:
                        settled(container)
                except (OSError, subprocess.SubprocessError):
                    extraction['warnings'].append('container_cleanup_unconfirmed')
    if extraction['error_code'] is None and extraction['status'] != 'extracted':
        extraction['error_code'] = 'empty_or_unavailable_output'
    ocr.write_private(out / 'sparse-ocr.json', attempt)
    return attempt


def _removed(container):
    result = subprocess.run(['docker', 'ps', '-a', '--no-trunc', '--filter', f'id={container}', '--format', '{{.ID}}'],
                            capture_output=True, timeout=15, check=False)
    return result.returncode == 0 and not result.stdout.strip()


def _read_results(folder, extraction, max_pages):
    try:
        receipt = json.loads(ocr.read_limited(folder / 'receipt.json', 1024 * 1024))
        count = receipt['page_count']; pages = receipt['pages']
        if not isinstance(count, int) or count < 0 or not isinstance(pages, list) or len(pages) > min(count, max_pages):
            raise ValueError('invalid receipt')
        parts = []; boundaries = []; size = 0; layout_size = 0; layout_omitted = 0
        for index, page in enumerate(pages, 1):
            if not isinstance(page, dict) or page.get('page') != index:
                raise ValueError('invalid page order')
            page = dict(page)
            data = b''
            path = folder / f'page-{index}.txt'
            if 'text_bytes' in page:
                data = ocr.read_limited(path, MAX_TEXT - size)
                if len(data) != page['text_bytes']:
                    raise ValueError('page size mismatch')
            if size + layout_size + len(data) > MAX_TEXT:
                raise ValueError('aggregate output limit')
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                page['discarded_text_bytes'] = len(data)
                page.pop('text_bytes', None)
                if page.get('error'):
                    page['prior_page_error'] = page['error']
                page['error'] = 'invalid_utf8_text_discarded'
                data = b''
                text = ''
            start = size; size += len(data)
            parts.append(text)
            boundary = {**page, 'text_start_byte': start, 'text_end_byte': size}
            if 'layout_bytes' in page:
                layout_data = ocr.read_limited(folder / f'page-{index}.tsv', MAX_LAYOUT - layout_size)
                if size + layout_size + len(layout_data) > MAX_TEXT:
                    raise ValueError('aggregate output limit')
                if len(layout_data) != page['layout_bytes']:
                    raise ValueError('layout size mismatch')
                try:
                    layout = layout_data.decode('utf-8')
                except UnicodeDecodeError:
                    boundary['layout_error'] = 'invalid_utf8_layout_discarded'
                    layout_omitted += 1
                else:
                    boundary.update(layout_tsv=layout, layout_sha256=ocr.digest(layout_data))
                    # JSON escaping can enlarge TSV; bound actual event-header metadata too.
                    prospective = json.dumps(boundaries + [boundary], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                    if len(prospective) > MAX_METADATA:
                        boundary.pop('layout_tsv'); boundary.pop('layout_sha256')
                        boundary['layout_error'] = 'metadata_limit'
                        layout_omitted += 1
                    else:
                        layout_size += len(layout_data)
                        if page.get('error') or page.get('worker_exit') != 0:
                            boundary['layout_error'] = 'layout_partial'
                            layout_omitted += 1
            else:
                boundary['layout_error'] = boundary.get('layout_error', 'layout_unavailable')
                layout_omitted += 1
            boundaries.append(boundary)
        text = ''.join(parts)
        extraction.update(page_count=count, pages=boundaries, omitted_pages=count - len(pages),
                          layout_omitted_pages=layout_omitted + count - len(pages))
        if extraction['layout_omitted_pages']:
            extraction['warnings'].append('layout_omitted')
        if any(p.get('error') or p.get('worker_exit') != 0 for p in boundaries):
            extraction['warnings'].append('page_failure_or_partial_output')
        if len(pages) < count:
            extraction['warnings'].append('pages_omitted')
        if text.strip():
            extraction.update(status='extracted', text=text, text_sha256=ocr.digest(text.encode('utf-8')))
        elif text:
            for boundary in boundaries:
                boundary['text_start_byte'] = boundary['text_end_byte'] = 0
            extraction['warnings'].append('whitespace_only_text_discarded')
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        extraction['error_code'] = 'invalid_or_unavailable_worker_output'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--anydoc-attempt', required=True)
    parser.add_argument('--poppler-attempt', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--max-pages', type=int, required=True)
    parser.add_argument('--trigger-policy', required=True)
    args = parser.parse_args()
    try:
        result = run(args.input, [args.anydoc_attempt, args.poppler_attempt], args.out_dir,
                     max_pages=args.max_pages, trigger_policy=args.trigger_policy)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print('Sparse OCR refused or failed; no document content printed.', file=sys.stderr)
        return 1
    print('Sparse OCR attempt recorded:', result['extraction']['status'])
    return 0 if result['extraction']['status'] == 'extracted' else 2


if __name__ == '__main__':
    sys.exit(main())
