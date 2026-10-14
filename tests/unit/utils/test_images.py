"""Tests for the AIM fork image budget (utils/images.py)."""

import base64
import io

import pytest
from PIL import Image, ImageDraw, ImageFilter

from mcp_atlassian.utils import images


def _png(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def _screenshot(size=(1920, 1080)) -> bytes:
    """UI-like: flat panels, many text-like strokes, a noisy photo area."""
    img = Image.new("RGB", size, (245, 246, 248))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 240, size[1]), fill=(30, 40, 60))
    for y in range(40, size[1] - 20, 22):
        for x in range(280, size[0] - 400, 90):
            d.text((x, y), "Running 5", fill=(20, 20, 20))
    noise = Image.merge("RGB", [Image.effect_noise((360, 300), 80) for _ in range(3)])
    img.paste(noise.filter(ImageFilter.GaussianBlur(1)), (size[0] - 380, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _photo(size=(3000, 2000)) -> bytes:
    img = Image.merge("RGB", [Image.effect_noise(size, 90) for _ in range(3)])
    buf = io.BytesIO()
    img.filter(ImageFilter.GaussianBlur(2)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _item(name: str, data: bytes, mime: str = "image/png"):
    return (name, base64.b64encode(data).decode(), mime)


def _size(b64: str) -> int:
    return len(base64.b64decode(b64))


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("IMAGE_BUDGET_BYTES", raising=False)
    item = _item("a.png", _screenshot())
    kept, skipped = images.fit_images([item])
    assert kept == [(item[0], item[1], item[2], "")]
    assert skipped == []


def test_small_image_passes_unchanged(monkeypatch):
    monkeypatch.setenv("IMAGE_BUDGET_BYTES", "90000")
    data = _png()
    kept, _ = images.fit_images([_item("a.png", data)])
    assert base64.b64decode(kept[0][1]) == data
    assert "as is" in kept[0][3]


def test_screenshots_split_budget(monkeypatch):
    monkeypatch.setenv("IMAGE_BUDGET_BYTES", "90000")
    shots = [_item(f"s{i}.png", _screenshot()) for i in range(2)]
    assert all(_size(s[1]) > 45000 for s in shots)
    kept, skipped = images.fit_images(shots)
    assert skipped == []
    assert [k[0] for k in kept] == ["s0.png", "s1.png"]
    assert all(_size(k[1]) <= 45000 for k in kept)
    assert all("KB ->" in k[3] for k in kept)


def test_photo_stays_jpeg(monkeypatch):
    monkeypatch.setenv("IMAGE_BUDGET_BYTES", "30000")
    kept, _ = images.fit_images([_item("p.jpg", _photo(), "image/jpeg")])
    assert kept[0][2] == "image/jpeg"
    assert _size(kept[0][1]) <= 30000
    assert "downscaled from 3000x2000" in kept[0][3]


def test_too_many_images_are_skipped(monkeypatch):
    monkeypatch.setenv("IMAGE_BUDGET_BYTES", "45000")
    monkeypatch.setenv("IMAGE_MIN_BYTES", "15000")
    items = [_item(f"i{i}.png", _png((200, 100))) for i in range(5)]
    kept, skipped = images.fit_images(items)
    assert [k[0] for k in kept] == ["i0.png", "i1.png", "i2.png"]
    assert [s["filename"] for s in skipped] == ["i3.png", "i4.png"]
    assert "separate step" in skipped[0]["error"]


def test_unopenable_passes_only_when_small(monkeypatch):
    monkeypatch.setenv("IMAGE_BUDGET_BYTES", "20000")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    kept, skipped = images.fit_images(
        [
            _item("a.svg", svg, "image/svg+xml"),
            _item("b.svg", svg * 1000, "image/svg+xml"),
        ]
    )
    assert [k[0] for k in kept] == ["a.svg"]
    assert skipped[0]["filename"] == "b.svg"


def test_impossible_budget_raises():
    noise = Image.merge(
        "RGB", [Image.effect_noise((2000, 2000), 120) for _ in range(3)]
    )
    buf = io.BytesIO()
    noise.save(buf, "PNG")
    with pytest.raises(images.ImageError, match="cannot be shrunk"):
        images.prepare(buf.getvalue(), 4096)


def test_pixel_bomb_refused():
    buf = io.BytesIO()
    Image.new("1", (8000, 6000)).save(buf, "PNG")
    with pytest.raises(images.ImageError, match="too large"):
        images.prepare(buf.getvalue(), 30000)
