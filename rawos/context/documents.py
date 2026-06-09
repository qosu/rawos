"""
rawos Document Connector — Phase 4.

Extracts plain text from PDF and DOCX files when they change.
Text excerpts are stored in context_events so the proactive agent can
reason about document content — not just code.

This makes rawos useful for lawyers, consultants, students, anyone who
works with documents rather than code.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("rawos.context.documents")

_DOCUMENT_EXTENSIONS = frozenset([".pdf", ".docx", ".doc"])
_MAX_TEXT_CHARS      = 4000   # excerpt limit stored in context_events
_EXCERPT_PREVIEW     = 500    # characters for the summary preview

# In-memory cache: file_path → (content_hash, full_text) to detect changes
_text_cache: dict[str, tuple[str, str]] = {}


def is_document(path: str) -> bool:
    """Return True if path is a supported document format."""
    p = Path(path)
    # Ignore hidden and build directories (same policy as _is_semantic in collector)
    _IGNORE_PREFIXES = (".", "__pycache__", "node_modules", ".git", "dist", "build")
    for part in p.parts:
        if any(part.startswith(pfx) for pfx in _IGNORE_PREFIXES):
            return False
    return p.suffix.lower() in _DOCUMENT_EXTENSIONS


def extract_text(path: str) -> str | None:
    """
    Extract plain text from a PDF or DOCX file.
    Returns None if the file cannot be read or library is missing.
    """
    ext = Path(path).suffix.lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(path)
        elif ext in (".docx", ".doc"):
            return _extract_docx(path)
    except Exception:
        log.debug("text extraction failed for %s", path, exc_info=True)
    return None


def _extract_pdf(path: str) -> str | None:
    try:
        from pdfminer.high_level import extract_text as pdf_extract_text
        text = pdf_extract_text(path)
        if not text:
            return None
        return text.strip()[:_MAX_TEXT_CHARS]
    except ImportError:
        log.debug("pdfminer.six not installed — PDF extraction unavailable")
        return None


def _extract_docx(path: str) -> str | None:
    try:
        import docx
        document = docx.Document(path)
        paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
        text = "\n".join(paragraphs)
        return text.strip()[:_MAX_TEXT_CHARS] if text else None
    except ImportError:
        log.debug("python-docx not installed — DOCX extraction unavailable")
        return None


def get_document_context(file_path: str) -> dict[str, Any] | None:
    """
    Extract text from document and return context metadata if content changed.
    Returns None if content is unchanged, extraction fails, or library missing.

    Thread-safe for single-threaded watchdog observer.
    """
    text = extract_text(file_path)
    if not text:
        return None

    text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    prev = _text_cache.get(file_path)
    if prev and prev[0] == text_hash:
        return None  # no change since last check

    _text_cache[file_path] = (text_hash, text)

    # Build excerpt for the context event metadata
    excerpt = text[:_EXCERPT_PREVIEW].replace("\n", " ").strip()

    return {
        "doc_text_excerpt": excerpt,
        "doc_text_hash":    text_hash,
        "doc_char_count":   len(text),
        "source_type":      "document",
    }
