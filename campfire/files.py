"""Serve stored files with byte ranges and safe dispositions."""

import re
from pathlib import Path

from starlette.responses import FileResponse, Response

from .http import not_found
from .request_context import RequestContext

IN_MEMORY = 1024 * 1024
RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def disposition_header(disposition: str, filename: str) -> str:
    safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
    kind = "attachment" if disposition == "attachment" else "inline"
    return f'{kind}; filename="{safe}"'


def serve_file(
    ctx: RequestContext,
    path: Path,
    content_type: str,
    filename: str,
    disposition: str = "inline",
    extra: dict[str, str] | None = None,
) -> Response:
    try:
        size = path.stat().st_size
    except OSError:
        not_found()
    headers = {
        "accept-ranges": "bytes",
        "content-disposition": disposition_header(disposition, filename),
        **(extra or {}),
    }
    requested = ctx.header("range")
    if requested:
        match = RANGE.fullmatch(requested)
        unsatisfiable = Response(status_code=416, headers={"content-range": f"bytes */{size}"})
        if not match or not (match[1] or match[2]):
            return unsatisfiable
        begin = int(match[1]) if match[1] else max(0, size - int(match[2] or 0))
        end = min(size - 1, int(match[2])) if match[1] and match[2] else size - 1
        if begin > end or begin >= size:
            return unsatisfiable
        with path.open("rb") as handle:
            handle.seek(begin)
            raw = handle.read(end - begin + 1)
        headers["content-range"] = f"bytes {begin}-{end}/{size}"
        return Response(raw, status_code=206, media_type=content_type, headers=headers)
    if size <= IN_MEMORY:
        return Response(path.read_bytes(), media_type=content_type, headers=headers)
    return FileResponse(path, media_type=content_type, headers=headers)
