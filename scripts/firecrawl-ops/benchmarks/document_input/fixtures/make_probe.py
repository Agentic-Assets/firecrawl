"""Recreate the owned image-only qualification artwork; requires Pillow/Poppler.

PDF metadata and font rendering vary. Regeneration creates a new fixture hash;
it never replaces the committed fixture or silently changes a sealed trial.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LINES = [
    (100, "DOCUMENT CAPABILITY PROBE"),
    (220, "Verification marker: CEDAR-9472"),
    (380, "Unit       Annual Rent USD"),
    (480, "ALPHA      41327"),
    (580, "BETA       86519"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--font",
        type=Path,
        required=True,
        help="path to a locally licensed TTF/TTC font",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="new PDF path; existing files are refused",
    )
    args = parser.parse_args()
    image = Image.new("RGB", (1600, 1000), "white")
    draw, font = ImageDraw.Draw(image), ImageFont.truetype(str(args.font), 40)
    for y, text in LINES:
        draw.text((100, y), text, font=font, fill="black")
    data = io.BytesIO()
    image.save(data, "PDF", resolution=150)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data.getvalue())
    extracted = subprocess.run(
        ["pdftotext", str(args.out), "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    if extracted.strip():
        raise ValueError("fixture unexpectedly has a text layer")
    print(
        json.dumps(
            {
                "sha256": hashlib.sha256(data.getvalue()).hexdigest(),
                "bytes": len(data.getvalue()),
                "text_layer_empty": True,
            }
        )
    )


if __name__ == "__main__":
    main()
