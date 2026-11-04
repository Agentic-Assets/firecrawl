"""Offline PDFium rendering and sparse Tesseract OCR, run inside the pinned image."""
import errno
import json
import math
import os
import resource
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pypdfium2 as pdfium

OUTPUT = Path('/output')
MAX_TEXT = 4 * 1024 * 1024
MAX_LAYOUT = 1024 * 1024
SCALE = 200 / 72


def _limit_ocr_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TEXT, MAX_TEXT))


def page(number):
    pdf = pdfium.PdfDocument('/input.pdf')
    item = pdf[number - 1]
    width, height = item.get_size()
    pixels = math.ceil(width * SCALE) * math.ceil(height * SCALE)
    if pixels > 25_000_000:
        print(json.dumps({'error': 'page_pixel_limit', 'pixels': pixels}))
        return 3
    image = f'/tmp/page-{number}.png'
    try:
        item.render(scale=SCALE).to_pil().save(image)
    except OSError as error:
        print(json.dumps({'error': 'image_file_limit' if error.errno == errno.EFBIG else 'image_render_error'}))
        return 4
    result = subprocess.run(['tesseract', image, str(OUTPUT / f'page-{number}'), '-l', 'eng', '--psm', '11', 'txt', 'tsv'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, preexec_fn=_limit_ocr_output)
    os.unlink(image)
    print(json.dumps({'pixels': pixels, 'tesseract_exit': result.returncode}))
    return result.returncode


def save(receipt):
    temp = OUTPUT / 'receipt.tmp'
    temp.write_text(json.dumps(receipt), encoding='utf-8')
    os.replace(temp, OUTPUT / 'receipt.json')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--page':
        return page(int(sys.argv[2]))
    limit = int(sys.argv[1])
    pdf = pdfium.PdfDocument('/input.pdf')
    count = len(pdf)
    receipt = {'page_count': count, 'pages': [], 'omitted_pages': max(0, count - limit)}
    save(receipt)
    total = 0
    layout_total = 0
    for number in range(1, min(count, limit) + 1):
        started = time.monotonic()
        info = {'page': number, 'started_at': datetime.now(timezone.utc).isoformat()}
        try:
            child = subprocess.Popen([sys.executable, __file__, '--page', str(number)], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                stdout, _ = child.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
                raise
            result = subprocess.CompletedProcess(child.args, child.returncode, stdout)
            try:
                info.update(json.loads(result.stdout))
            except (ValueError, UnicodeError):
                info['error'] = 'invalid_page_diagnostic'
            info['worker_exit'] = result.returncode
        except subprocess.TimeoutExpired:
            info['error'] = 'page_timeout'
        info['elapsed_seconds'] = round(time.monotonic() - started, 3)
        path = OUTPUT / f'page-{number}.txt'
        if path.exists():
            size = path.stat().st_size
            if size > MAX_TEXT - total - layout_total:
                path.unlink()
                info['error'] = 'text_limit'
                receipt['omitted_pages'] += min(count, limit) - number
                receipt['pages'].append(info)
                save(receipt)
                break
            total += size
            info['text_bytes'] = size
        layout = OUTPUT / f'page-{number}.tsv'
        if layout.exists():
            layout_size = layout.stat().st_size
            if layout_size > min(MAX_LAYOUT - layout_total, MAX_TEXT - total - layout_total):
                layout.unlink()
                info['layout_error'] = 'layout_limit'
            else:
                layout_total += layout_size
                info['layout_bytes'] = layout_size
        else:
            info['layout_error'] = 'layout_unavailable'
        receipt['pages'].append(info)
        save(receipt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
