"""Document attachments (PDF, DOCX, PPTX, XLSX, HTML) as markdown via docling-serve.

Clients whose model accepts only text and images (e.g. an OpenAI-compatible
vLLM endpoint behind LibreChat) cannot read office files returned as
base64 embedded resources. The ``*_documents`` tools instead send each
document to a docling-serve instance (``DOCLING_URL``) and return the
resulting markdown with tables preserved. Without ``DOCLING_URL`` the tools
report that conversion is not configured.

Documents of one call are converted concurrently (``DOCLING_CONCURRENCY``,
default 3): docling-serve runs several conversions at once, and a call with
six attachments otherwise waits for the sum of all of them.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

# Extension → MIME. Extension wins: Confluence and Jira often report office
# files as application/octet-stream.
_OOXML = "application/vnd.openxmlformats-officedocument"
_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": f"{_OOXML}.wordprocessingml.document",
    ".pptx": f"{_OOXML}.presentationml.presentation",
    ".xlsx": f"{_OOXML}.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
}
_DOCUMENT_MIME_TYPES = frozenset(_DOCUMENT_TYPES.values())


def docling_url() -> str:
    return os.getenv("DOCLING_URL", "").rstrip("/")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def document_max_chars() -> int:
    """Per-document cap on returned markdown (~1 token per 3-4 chars)."""
    return _env_int("DOCUMENT_MAX_CHARS", 200_000)


def document_concurrency() -> int:
    """How many documents of one call are fetched and converted at once."""
    return max(1, _env_int("DOCLING_CONCURRENCY", 3))


_T = TypeVar("_T")
_R = TypeVar("_R")


async def gather_limited(
    items: Iterable[_T], worker: Callable[[_T], Awaitable[_R]]
) -> list[_R]:
    """Run ``worker`` over ``items`` with at most ``document_concurrency()``
    in flight; results keep the order of ``items``."""
    sem = asyncio.Semaphore(document_concurrency())

    async def run(item: _T) -> _R:
        async with sem:
            return await worker(item)

    return list(await asyncio.gather(*(run(item) for item in items)))


class DocumentConversionError(Exception):
    """docling-serve failed or returned no usable result."""


def is_document_attachment(
    media_type: str | None, filename: str | None
) -> tuple[bool, str]:
    """Detect whether an attachment is a document docling can convert.

    Args:
        media_type: The MIME type reported by the API.
        filename: The attachment filename.

    Returns:
        Tuple of (is_document, resolved_mime_type).
    """
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext in _DOCUMENT_TYPES:
            return True, _DOCUMENT_TYPES[ext]
    if media_type in _DOCUMENT_MIME_TYPES:
        return True, media_type
    return False, media_type or "application/octet-stream"


async def convert_document(data: bytes, filename: str, mime_type: str) -> str:
    """Convert a document to markdown with docling-serve.

    Pictures inside the document become ``<!-- image -->`` placeholders;
    OCR runs only on pages without a text layer (scans).

    Args:
        data: Raw document bytes.
        filename: Attachment filename (sent to docling for format detection).
        mime_type: Resolved document MIME type.

    Returns:
        Markdown, truncated to ``DOCUMENT_MAX_CHARS`` with a note.

    Raises:
        DocumentConversionError: docling is unreachable or the conversion failed.
    """
    url = docling_url()
    if not url:
        raise DocumentConversionError("document conversion is not configured")
    form = {
        "to_formats": "md",
        "image_export_mode": "placeholder",
        "do_ocr": "true",
        "ocr_engine": os.getenv("DOCLING_OCR_ENGINE", "tesseract"),
        "ocr_lang": [
            lang.strip()
            for lang in os.getenv("DOCLING_OCR_LANG", "rus,eng").split(",")
            if lang.strip()
        ],
        "abort_on_error": "false",
    }
    timeout = httpx.Timeout(float(os.getenv("DOCLING_TIMEOUT", "600")), connect=10.0)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{url}/v1/convert/file",
                data=form,
                files={"files": (filename, data, mime_type)},
            )
    except httpx.HTTPError as e:
        msg = f"docling unreachable: {type(e).__name__}: {e}"
        raise DocumentConversionError(msg) from e
    if resp.status_code != 200:
        msg = f"docling HTTP {resp.status_code}: {resp.text[:300]}"
        raise DocumentConversionError(msg)
    try:
        result = resp.json()
        markdown = (result.get("document") or {}).get("md_content") or ""
    except (ValueError, AttributeError) as e:
        msg = f"unexpected docling response: {e}"
        raise DocumentConversionError(msg) from e
    # partial_success: some pages failed, the rest is still useful.
    if result.get("status") not in ("success", "partial_success"):
        msg = (
            f"docling status={result.get('status')} "
            f"errors={str(result.get('errors'))[:300]}"
        )
        raise DocumentConversionError(msg)
    markdown = markdown.strip()
    limit = document_max_chars()
    if len(markdown) > limit:
        markdown = (
            markdown[:limit]
            + f"\n\n[document truncated: first {limit} of {len(markdown)} characters]"
        )
    logger.info(
        "docling converted '%s' (%d bytes, status=%s, %d chars) in %.1fs",
        filename,
        len(data),
        result.get("status"),
        len(markdown),
        time.monotonic() - started,
    )
    return markdown


def document_text(filename: str, markdown: str) -> str:
    """Wrap converted markdown so the model knows what it is reading."""
    if not markdown:
        markdown = (
            "_No text found (the document may consist of images only). "
            "Ask the user for screenshots of the relevant pages._"
        )
    return (
        f"## Attachment: {filename}\n"
        "_Text extracted by docling, not the original file: pictures, charts and "
        "diagrams are not included (shown as <!-- image -->)._\n\n"
        f"{markdown}"
    )
