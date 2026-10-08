"""Typed boundary around pyvips (libvips ships without type information)."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from pathlib import Path

import pyvips

pyvips.operation_block_set("VipsForeignLoadOpenslide", True)
pyvips.block_untrusted_set(True)
_SHARPEN = pyvips.Image.new_from_array(
    [[-1, -1, -1], [-1, 32, -1], [-1, -1, -1]], scale=24, offset=0
)


def analyze_image(path: Path) -> tuple[int, int]:
    image = pyvips.Image.new_from_file(str(path), access="sequential").autorot()
    return int(image.width), int(image.height)


def thumbnail(path: Path, size: tuple[int, int], fmt: str) -> tuple[bytes, int, int]:
    """Active Storage's resize_to_limit with its default sharpening, encoded as `fmt`."""
    image = pyvips.Image.new_from_file(str(path), access="sequential").autorot()
    image = image.thumbnail_image(size[0], height=size[1], size="down", no_rotate=True)
    image = image.conv(_SHARPEN, precision="integer")
    # Adaptive PNG filtering (libpng's default); the bundled encoder otherwise skips it.
    raw = (
        image.write_to_buffer("." + fmt, filter="all")
        if fmt == "png"
        else image.write_to_buffer("." + fmt)
    )
    return bytes(raw), int(image.width), int(image.height)


def convert_to_webp(path: Path) -> bytes:
    return bytes(pyvips.Image.new_from_file(str(path)).write_to_buffer(".webp"))
