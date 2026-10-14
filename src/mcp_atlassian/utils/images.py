"""Shrink inline images so that a chat client's tool-result budget keeps them.

AIM fork. LibreChat (@librechat/agents 4.x) counts base64 image data as
characters against the endpoint's ``maxToolResultChars`` and silently
replaces an image that does not fit with "[omitted N oversized atomic
tool-content block: image_url]": the user still sees it in the chat, the
model does not. For OpenAI-like providers all images of one agent step share
that single budget. A plain 1920x1080 screenshot is 100-500 KB (130-670k
base64 characters), so two of them already overflow a 130k budget.

``IMAGE_BUDGET_BYTES`` (env, default 0 = disabled, upstream behaviour) is the
total size of all images returned by one tool call. It is split evenly
between the images; each is passed through when it already fits, otherwise
re-encoded and downscaled until it does:

- screenshots, charts (png, gif, bmp): 256-colour png first (flat UI colours
  and text stay crisp), then jpeg 80 / 60;
- photos (jpeg, webp): jpeg 85 / 70 / 55;
- long side 2048 -> 1600 -> 1280 -> 1024 -> 800 -> 640 -> 512.

When a share would fall below ``IMAGE_MIN_BYTES`` (default 15000) only the
first images that fit are returned; the rest are reported as skipped, so the
model asks for them in a later step instead of getting a placeholder.
Images that Pillow cannot open (svg) are passed through only when they fit.
"""

from __future__ import annotations

import base64
import io
import logging
import os

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

_PASS_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_PHOTO_FORMATS = ("JPEG", "WEBP")
_SIDES = (2048, 1600, 1280, 1024, 800, 640, 512)
# Never decode more pixels than this (decompression bomb guard).
_MAX_PIXELS = 40_000_000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def image_budget() -> int:
    """Total image bytes per tool call; 0 disables shrinking."""
    return max(0, _env_int("IMAGE_BUDGET_BYTES", 0))


def min_image_bytes() -> int:
    return max(4096, _env_int("IMAGE_MIN_BYTES", 15000))


class ImageError(RuntimeError):
    """The image cannot be made to fit."""


def _encode(img: Image.Image, kind: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if kind == "png256":
        src = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA")
        method = (
            Image.Quantize.FASTOCTREE
            if src.mode == "RGBA"
            else Image.Quantize.MEDIANCUT
        )
        src.quantize(256, method=method).save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    quality = int(kind[4:])  # "jpeg85"
    flat = img
    if img.mode in ("RGBA", "LA"):
        # jpeg has no alpha: a transparent background would turn black.
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
    elif img.mode != "RGB":
        flat = img.convert("RGB")
    flat.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"


def prepare(data: bytes, budget: int) -> tuple[bytes, str, str]:
    """Image bytes -> (bytes within budget, mime, note). Raises ImageError."""
    # Own pixel limit instead of Pillow's warning/exception: open() reads the
    # header only, so the check happens before any decoding.
    Image.MAX_IMAGE_PIXELS = None
    try:
        img: Image.Image = Image.open(io.BytesIO(data))
        fmt = img.format or "?"
        w, h = img.size
        if w * h > _MAX_PIXELS:
            msg = f"{w}x{h} is too large to decode"
            raise ImageError(msg)
        frames = getattr(img, "n_frames", 1)
        if (
            fmt in _PASS_FORMATS
            and max(w, h) <= _SIDES[0]
            and frames == 1
            and len(data) <= budget
        ):
            return data, _PASS_FORMATS[fmt], f"{w}x{h} {fmt}, as is"

        img.seek(0)
        img.load()
        notes = [f"first frame of {frames}"] if frames > 1 else []
        # Convert before resizing: LANCZOS does not smooth palette/16-bit.
        if img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        photo = fmt in _PHOTO_FORMATS
        kinds = (
            ("jpeg85", "jpeg70", "jpeg55") if photo else ("png256", "jpeg80", "jpeg60")
        )
        smallest = None  # (bytes, long side) of the best failed attempt
        for side in _SIDES:
            if max(w, h) < side and side != _SIDES[0]:
                continue
            # Size grows roughly with area: skip steps that cannot fit.
            if smallest and smallest[0] * (side / smallest[1]) ** 2 > budget * 1.3:
                continue
            if max(w, h) > side:
                cur = img.copy()
                cur.thumbnail((side, side), Image.Resampling.LANCZOS)
            else:
                cur = img
            for kind in kinds:
                out, mime = _encode(cur, kind)
                if len(out) <= budget:
                    if cur.size != (w, h):
                        notes.append(f"downscaled from {w}x{h}")
                    notes.append(f"{len(data) // 1024} KB -> {len(out) // 1024} KB")
                    label = "PNG-256" if kind == "png256" else "JPEG"
                    note = f"{cur.size[0]}x{cur.size[1]} {label} ({'; '.join(notes)})"
                    return out, mime, note
                if smallest is None or len(out) < smallest[0]:
                    smallest = (len(out), max(cur.size))
        msg = f"{w}x{h} cannot be shrunk to {budget // 1024} KB"
        raise ImageError(msg)
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as e:
        msg = f"not a raster image Pillow can open: {e}"
        raise ImageError(msg) from e


def fit_images(
    items: list[tuple[str, str, str]],
) -> tuple[list[tuple[str, str, str, str]], list[dict[str, object]]]:
    """Fit base64 images of one tool call into ``image_budget()``.

    Args:
        items: ``(filename, base64_data, mime_type)`` in output order.

    Returns:
        ``(kept, skipped)``: kept as ``(filename, base64_data, mime, note)``,
        skipped as summary entries with the reason. With the budget disabled
        everything is kept unchanged with an empty note.
    """
    budget = image_budget()
    if not budget or not items:
        return [(name, data, mime, "") for name, data, mime in items], []
    count = min(len(items), max(1, budget // min_image_bytes()))
    share = budget // count
    kept: list[tuple[str, str, str, str]] = []
    skipped: list[dict[str, object]] = [
        {
            "filename": name,
            "error": (
                f"not returned: {count} images per call fit the chat budget; "
                "request it in a separate step"
            ),
        }
        for name, _, _ in items[count:]
    ]
    for name, data, mime in items[:count]:
        raw = base64.b64decode(data)
        try:
            out, out_mime, note = prepare(raw, share)
        except ImageError as e:
            if len(raw) <= share:
                kept.append((name, data, mime, "as is"))
            else:
                skipped.append({"filename": name, "error": f"not returned: {e}"})
            continue
        logger.info("image '%s': %d bytes -> %s", name, len(raw), note)
        encoded = data if out is raw else base64.b64encode(out).decode("ascii")
        kept.append((name, encoded, out_mime, note))
    return kept, skipped
