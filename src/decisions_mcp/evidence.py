"""Convert evidence into API content parts without coupling to a data source."""

import base64
import json
from pathlib import Path

from .models import (
    ImagePathEvidence,
    ImageUrlEvidence,
    JsonEvidence,
    TextEvidence,
    TextPathEvidence,
)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024


def read_local(path: str, roots: tuple[Path, ...]) -> bytes:
    target = Path(path).expanduser().resolve()
    if not any(target.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("Local file is outside DECISIONS_ALLOWED_ROOTS")
    if not target.is_file():
        raise ValueError("Local evidence must be a regular file")
    # Read bounded data even if the file changes after stat().
    with target.open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Local evidence exceeds the 20 MiB file limit")
    return data


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Supported local image formats: PNG, JPEG, GIF, WebP")


def prepare(evidence, roots: tuple[Path, ...]):
    parts = []
    cacheable = True
    for item in evidence:
        if isinstance(item, TextEvidence):
            parts.append({"type": "input_text", "text": item.text})
        elif isinstance(item, JsonEvidence):
            parts.append({"type": "input_json", "data": item.data})
        elif isinstance(item, TextPathEvidence):
            try:
                text = read_local(item.path, roots).decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("Text files must be UTF-8") from None
            parts.append({"type": "input_text", "text": text})
        elif isinstance(item, ImagePathEvidence):
            data = read_local(item.path, roots)
            encoded = base64.b64encode(data).decode("ascii")
            parts.append(
                {
                    "type": "input_image",
                    "detail": item.detail,
                    "image_url": f"data:{image_mime(data)};base64,{encoded}",
                }
            )
        elif isinstance(item, ImageUrlEvidence):
            if item.url.startswith(("http://", "https://")):
                # A URL can serve changed bytes; don't reuse an old result.
                cacheable = False
            else:
                try:
                    header, encoded = item.url.split(",", 1)
                    if ";base64" not in header:
                        raise ValueError()
                    data = base64.b64decode(encoded, validate=True)
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError()
                    mime = image_mime(data)
                    if header != f"data:{mime};base64":
                        raise ValueError()
                except (ValueError, base64.binascii.Error):
                    raise ValueError("Expected a valid base64 PNG/JPEG/GIF/WebP data URL") from None
            parts.append({"type": "input_image", "detail": item.detail, "image_url": item.url})
    if len(json.dumps(parts, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        raise ValueError("Evidence exceeds the 32 MiB wrapper request limit")
    return parts, cacheable
