"""Real encoded test media generated with Pillow."""

import io

from PIL import Image


def png(width: int, height: int, color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, "PNG")
    return output.getvalue()


def pdf(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, "PDF")
    return output.getvalue()
