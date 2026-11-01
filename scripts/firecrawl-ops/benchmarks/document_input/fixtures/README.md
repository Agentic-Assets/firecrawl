Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134).

# Owned PDF capability fixture

`synthetic-image-only.pdf` was generated locally from synthetic labels and amounts
using Pillow, a 1600×1000 white RGB canvas, Helvetica at 40 pt and a 150 DPI PDF
export. It has one page and no extractable text layer. It contains no third-party
document content. Its SHA-256 is
`d64dfdb48b80399a628b1bcb1459ec050efd64cf935f731777cec698f148c04f`.

`make_probe.py --font /path/to/font.ttf --out /private/new-probe.pdf` preserves
the generating method. It requires Pillow and Poppler. Fonts and PDF metadata
can change bytes, so a regenerated PDF has a new identity and needs a separately
reviewed fixture hash. The command refuses existing files. The benchmark always
uses the committed bytes and verifies their hash before qualification.

A passing response must reproduce marker `CEDAR-9472` and associate `ALPHA`
with annual rent `41327`, and `BETA` with `86519`. This checks that the chosen
model/provider route actually sees an image-only PDF. It does not establish
quality on long PDFs, diagrams, tables, or real listings.
