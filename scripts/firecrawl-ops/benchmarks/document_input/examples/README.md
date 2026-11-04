# Native PDF example

`native-pdf-grid.json` illustrates two effort levels for one pinned provider. Model availability, output caps and pricing can change; verify the route and choose an appropriate planning budget before any paid call.

Install Poppler `pdfinfo` and make it available on `PATH` for PDF page-count verification.

The following creates a disposable owner-only corpus using the repository's synthetic image-only fixture. Run from this directory; `WORK` must name a new directory outside the repository:

```sh
mkdir -m 700 "$WORK"
cp ../fixtures/synthetic-image-only.pdf "$WORK/synthetic.pdf"
python3 - "$WORK" <<'PY'
import hashlib, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
data = (root / 'synthetic.pdf').read_bytes()
manifest = {'schema': 'document-input-benchmark-corpus/v1', 'documents': [
    {'id': 'synthetic-01', 'path': 'synthetic.pdf',
     'sha256': hashlib.sha256(data).hexdigest(), 'pages': 1}]}
path = root / 'manifest.json'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump(manifest, out)
PY
python3 ../native_pdf.py prepare --config native-pdf-grid.json --manifest "$WORK/manifest.json" --out "$WORK/run"
python3 ../native_pdf.py qualify --out "$WORK/run"
python3 ../native_pdf.py run --out "$WORK/run"
python3 ../native_pdf.py report --out "$WORK/run"
```

The last two previews do not make network requests. The explicit `--execute` option on `qualify` or `run` authorizes paid calls; do not use it for a preview. Qualification must pass with verified provider receipts before the corpus run. `reconcile` performs GET lookups for existing calls. Report values cover format and usage, not semantic accuracy.


The canary generation method is in the adjacent `fixtures/README.md`.
