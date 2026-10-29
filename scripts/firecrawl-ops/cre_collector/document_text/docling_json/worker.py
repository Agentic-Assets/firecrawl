"""Prebuilt Docling worker. Executed only in the bounded offline container."""
import json
import sys
from pathlib import Path

import pypdfium2 as pdfium
from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import DoclingDocument


def main():
    maximum = int(sys.argv[1])
    if not 1 <= maximum <= 1000:
        raise ValueError('invalid page bound')
    source = Path('/input.pdf')
    count = len(pdfium.PdfDocument(source))
    if count < 1:
        raise ValueError('empty PDF')
    last = min(maximum, count)
    options = PdfPipelineOptions(artifacts_path=Path('/opt/app-root/src/.cache/docling/models'),
        enable_remote_services=False, allow_external_plugins=False, do_ocr=False, do_table_structure=True,
        document_timeout=90, accelerator_options=AcceleratorOptions(num_threads=1, device='cpu'),
        generate_page_images=False, generate_picture_images=False, generate_table_images=False)
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(source, page_range=(1, last), max_file_size=100 * 1024 * 1024)
    if result.status.value not in ('success', 'partial_success'):
        raise ValueError('conversion unsuccessful')
    value = result.document.export_to_dict()
    DoclingDocument.model_validate(value)  # Shadow validation only; retain the original export.
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('output exceeds limit')
    (Path('/output') / 'document.json').write_bytes(raw)
    (Path('/output') / 'receipt.json').write_text(json.dumps({'page_count': count, 'last_page': last, 'status': result.status.value}), encoding='utf-8')


if __name__ == '__main__':
    main()
