"""Tests for docling document conversion helpers."""

import httpx
import pytest

from mcp_atlassian.utils import documents
from mcp_atlassian.utils.documents import (
    DocumentConversionError,
    convert_document,
    document_text,
    is_document_attachment,
)

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def test_is_document_attachment():
    assert is_document_attachment("application/octet-stream", "Deck.PPTX") == (
        True,
        PPTX,
    )
    assert is_document_attachment("application/pdf", None) == (True, "application/pdf")
    assert is_document_attachment("image/png", "a.png") == (False, "image/png")
    assert is_document_attachment(None, "legacy.doc")[0] is False


@pytest.fixture
def docling(monkeypatch):
    state = {
        "requests": [],
        "reply": httpx.Response(
            200,
            json={
                "document": {"md_content": "# Title\n\n| a |\n|---|\n| 1 |"},
                "status": "success",
            },
        ),
    }

    def handler(request):
        state["requests"].append(request)
        return state["reply"]

    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setenv("DOCLING_URL", "http://docling:5001/")
    monkeypatch.setattr(documents.httpx, "AsyncClient", client_factory)
    return state


@pytest.mark.anyio
async def test_convert_document_request(docling):
    md = await convert_document(b"PK", "slides.pptx", PPTX)
    assert md == "# Title\n\n| a |\n|---|\n| 1 |"
    req = docling["requests"][0]
    assert str(req.url) == "http://docling:5001/v1/convert/file"
    body = req.content.decode("utf-8", "replace")
    for part in ('filename="slides.pptx"', "placeholder", "tesseract", "rus", "eng"):
        assert part in body


@pytest.mark.anyio
async def test_convert_document_truncates(docling, monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_CHARS", "5")
    docling["reply"] = httpx.Response(
        200, json={"document": {"md_content": "x" * 20}, "status": "partial_success"}
    )
    md = await convert_document(b"%PDF", "a.pdf", "application/pdf")
    assert md.startswith("xxxxx\n\n[document truncated: first 5 of 20")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500, text="oops"),
        httpx.Response(
            200, json={"document": {}, "status": "failure", "errors": ["bad"]}
        ),
        httpx.Response(200, text="not json"),
    ],
)
async def test_convert_document_errors(docling, reply):
    docling["reply"] = reply
    with pytest.raises(DocumentConversionError):
        await convert_document(b"%PDF", "a.pdf", "application/pdf")


@pytest.mark.anyio
async def test_convert_document_not_configured(monkeypatch):
    monkeypatch.delenv("DOCLING_URL", raising=False)
    with pytest.raises(DocumentConversionError, match="not configured"):
        await convert_document(b"%PDF", "a.pdf", "application/pdf")


def test_document_text():
    assert document_text("a.pdf", "body").startswith("## Attachment: a.pdf\n")
    assert "No text found" in document_text("scan.pdf", "")


@pytest.mark.anyio
async def test_gather_limited_order_and_limit(monkeypatch):
    import asyncio

    monkeypatch.setenv("DOCLING_CONCURRENCY", "2")
    running = peak = 0

    async def worker(n):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01 * (5 - n))  # later items finish first
        running -= 1
        return n * 10

    result = await documents.gather_limited(range(5), worker)
    assert result == [0, 10, 20, 30, 40]
    assert peak == 2


def test_document_concurrency_bad_values(monkeypatch):
    monkeypatch.setenv("DOCLING_CONCURRENCY", "0")
    assert documents.document_concurrency() == 1
    monkeypatch.setenv("DOCLING_CONCURRENCY", "x")
    assert documents.document_concurrency() == 3
