"""Exact Poppler complement modes and artifact configuration."""

TEXT = 'poppler_pdf_text_v1'
LAYOUT = 'poppler_pdf_layout_v1'
MODES = (TEXT, LAYOUT)


def config(mode, *, executable=None):
    if mode not in MODES:
        raise ValueError('invalid Poppler mode')
    result = {'ocr': False, 'timeout_seconds': 120,
              'max_output_file_bytes': 4 * 1024 * 1024, 'max_text_bytes': 4 * 1024 * 1024}
    if executable is not None:
        result['executable'] = executable
    if mode == LAYOUT:
        result['mode'] = LAYOUT
    return result


def mode_from_config(value):
    if not isinstance(value, dict):
        raise ValueError('invalid Poppler config')  # noqa: TRY004
    mode = value.get('mode', TEXT)
    if value != config(mode, executable=value.get('executable')):
        raise ValueError('invalid Poppler config')
    return mode
