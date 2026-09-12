"""Dedicated PDF editing backend for Skillo ID Print Solutions.

This service intentionally stays separate from pdf_crop_worker.py.  The crop
worker handles ID-card detection; this worker handles document editing and
returns a PDF without forcing the browser to do all heavy processing.

Endpoints:
  GET  /health
  GET  /capabilities
  POST /inspect-pdf   multipart: file, password, page (optional)
  POST /render-page   multipart: file, password, page, dpi (optional)
  POST /replace-text  multipart: file, password, page, old_text,
                       replacement, occurrence, font_file (optional)
  POST /redact-text   multipart: file, password, page, text/rect,
                       occurrence, mode (text|area)
  POST /ocr-pdf       multipart: file, password, language
  POST /preflight-pdf multipart: file, password
  POST /optimize-pdf  multipart: file, password (unsigned PDFs only)

Mutation endpoints return application/pdf bytes.  The frontend can replace
its in-memory PDF with the response Blob and continue using its existing
viewer.  All processing is local to this service; no PDF is uploaded to a
third-party document API.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

import fitz
from flask import Flask, jsonify, request, send_file


app = Flask(__name__)
SERVICE_VERSION = "1.2.0"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "150"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

ALLOWED_ORIGINS = {
    value.strip().rstrip("/")
    for value in os.getenv(
        "ALLOWED_ORIGINS",
        "https://idprintingportal.github.io,https://all-services-are-working-fine-2.onrender.com,http://localhost:3000,http://127.0.0.1:5500",
    ).split(",")
    if value.strip()
}
EDITOR_API_KEY = os.getenv("EDITOR_API_KEY", "").strip()
PORTAL_AUTH_URL = os.getenv("PORTAL_AUTH_URL", "").strip()


def _error(message: str, status: int = 400):
    return jsonify(success=False, error=message), status


@app.after_request
def security_headers(response):
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Editor-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def too_large(_error):
    return _error("PDF upload is too large.", 413)


@app.route("/<path:_path>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def options(_path=""):
    return ("", 204)


def require_auth():
    """Require the optional service key and/or portal bearer session."""
    if EDITOR_API_KEY and request.headers.get("X-Editor-Key", "") != EDITOR_API_KEY:
        return _error("Editor service authentication required.", 401)
    if not PORTAL_AUTH_URL:
        return None
    bearer = request.headers.get("Authorization", "")
    if not bearer.startswith("Bearer "):
        return _error("Login required.", 401)
    token = bearer[7:].strip()
    if len(token) != 64 or not re.fullmatch(r"[A-Za-z0-9]+", token):
        return _error("Invalid session.", 401)
    payload = json.dumps({"action": "getMe", "token": token}).encode("utf-8")
    try:
        req = urllib.request.Request(
            PORTAL_AUTH_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("success") or result.get("state") != "active":
            return _error("Active account required.", 403)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return _error("Session validation unavailable.", 503)
    return None


def _read_upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        raise ValueError("PDF file is required.")
    raw = uploaded.read()
    if not raw:
        raise ValueError("PDF file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("PDF upload is too large.")
    return raw, uploaded.filename


def _open_pdf(raw: bytes, password: str = ""):
    document = fitz.open(stream=raw, filetype="pdf")
    if document.needs_pass and not document.authenticate(password):
        document.close()
        raise PermissionError("PDF password is required or incorrect.")
    if document.page_count < 1:
        document.close()
        raise ValueError("PDF has no readable pages.")
    if document.page_count > MAX_PDF_PAGES:
        document.close()
        raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit.")
    return document


def _page_number(value: Any, page_count: int) -> int:
    try:
        number = int(value if value not in (None, "") else 0)
    except (TypeError, ValueError):
        raise ValueError("Page must be a number.") from None
    if number >= 1:
        number -= 1
    if number < 0 or number >= page_count:
        raise ValueError("Page is outside this PDF.")
    return number


def _rgb(value: Any) -> list[int]:
    color = int(value or 0)
    return [(color >> 16) & 255, (color >> 8) & 255, color & 255]


def _font_flags(flags: int, font_name: str) -> dict[str, bool]:
    lowered = font_name.lower()
    return {
        "bold": bool(flags & 16) or any(token in lowered for token in ("bold", "black", "heavy", "semibold")),
        "italic": bool(flags & 2) or any(token in lowered for token in ("italic", "oblique")),
        "serif": bool(flags & 4),
        "monospace": bool(flags & 8),
    }


def _text_spans(page: fitz.Page) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    data = page.get_text("dict", sort=False)
    for block_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            for span_index, span in enumerate(line.get("spans", [])):
                text = str(span.get("text", ""))
                bbox = span.get("bbox")
                if not text or not bbox or len(bbox) != 4:
                    continue
                font_name = str(span.get("font", ""))
                flags = int(span.get("flags", 0) or 0)
                style = _font_flags(flags, font_name)
                spans.append(
                    {
                        "block": block_index,
                        "line": line_index,
                        "span": span_index,
                        "text": text,
                        "bbox": [round(float(item), 3) for item in bbox],
                        "origin": [round(float(item), 3) for item in span.get("origin", (0, 0))],
                        "font": font_name,
                        "fontName": font_name,
                        "fontSize": round(float(span.get("size", 0) or 0), 3),
                        "size": round(float(span.get("size", 0) or 0), 3),
                        "flags": flags,
                        "color": _rgb(span.get("color", 0)),
                        "colorHex": "#{:06x}".format(int(span.get("color", 0) or 0) & 0xFFFFFF),
                        "bold": style["bold"],
                        "italic": style["italic"],
                        "serif": style["serif"],
                        "monospace": style["monospace"],
                    }
                )
    return spans


def _font_inventory(page: fitz.Page) -> list[dict[str, Any]]:
    result = []
    try:
        for entry in page.get_fonts(full=True):
            result.append(
                {
                    "xref": entry[0],
                    "type": entry[2],
                    "baseFont": entry[3],
                    "name": entry[4],
                    "encoding": entry[5],
                }
            )
    except Exception:
        pass
    return result


def _parse_rect(value: Any) -> fitz.Rect:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [item.strip() for item in value.split(",")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("rect must contain [x0, y0, x1, y1].")
    try:
        x0, y0, x1, y1 = [float(item) for item in value]
    except (TypeError, ValueError):
        raise ValueError("rect contains an invalid coordinate.") from None
    rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if rect.is_empty or rect.width < 0.5 or rect.height < 0.5:
        raise ValueError("rect is empty.")
    return rect


def _overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    intersection = a & b
    return max(0.0, intersection.get_area())


def _find_match(page: fitz.Page, old_text: str, occurrence: int = 0, rect: Any = None):
    if rect is not None:
        target = _parse_rect(rect)
        matches = [span for span in _text_spans(page) if _overlap(target, fitz.Rect(span["bbox"])) > 0]
        if not matches:
            raise ValueError("No text was found inside the supplied rect.")
        return target, matches[0]
    query = str(old_text or "").strip()
    if not query:
        raise ValueError("old_text or rect is required.")
    rectangles = page.search_for(query, quads=False)
    if not rectangles:
        normalized = re.sub(r"\s+", " ", query).strip().lower()
        for span in _text_spans(page):
            if re.sub(r"\s+", " ", span["text"]).strip().lower() == normalized:
                rectangles.append(fitz.Rect(span["bbox"]))
    if not rectangles:
        raise ValueError("Exact text match was not found on this page.")
    try:
        index = int(occurrence)
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index >= len(rectangles):
        raise ValueError("Text occurrence is outside the available matches.")
    target = fitz.Rect(rectangles[index])
    spans = _text_spans(page)
    candidates = [span for span in spans if _overlap(target, fitz.Rect(span["bbox"])) > 0]
    style = max(candidates, key=lambda span: _overlap(target, fitz.Rect(span["bbox"]))) if candidates else None
    return target, style


def _font_dirs() -> list[Path]:
    configured = [Path(value.strip()) for value in os.getenv("FONT_DIRS", "").split(os.pathsep) if value.strip()]
    defaults = [
        Path("/usr/share/fonts/truetype"),
        Path("/usr/share/fonts/opentype"),
        Path("C:/Windows/Fonts"),
    ]
    seen: set[str] = set()
    result: list[Path] = []
    for directory in configured + defaults:
        key = str(directory).lower()
        if key not in seen and directory.exists():
            result.append(directory)
            seen.add(key)
    return result


@lru_cache(maxsize=1)
def _font_catalog() -> list[Path]:
    files: list[Path] = []
    for directory in _font_dirs():
        try:
            files.extend(path for path in directory.rglob("*") if path.suffix.lower() in {".ttf", ".otf", ".ttc"})
        except OSError:
            continue
    return files


def _font_score(path: Path, original: str, bold: bool, italic: bool, devanagari: bool) -> int:
    name = re.sub(r"[^a-z0-9]", "", path.stem.lower())
    source = re.sub(r"[^a-z0-9]", "", original.lower())
    score = 0
    if source and (source in name or name in source):
        score += 100
    if devanagari and any(token in name for token in ("devanagari", "lohitdev", "noto", "mangal")):
        score += 50
    if bold == any(token in name for token in ("bold", "black", "heavy", "semibold")):
        score += 30
    if italic == any(token in name for token in ("italic", "oblique")):
        score += 20
    if not devanagari and any(token in name for token in ("notosans", "dejavusans", "arial", "liberationsans")):
        score += 10
    return score


def _resolve_font_file(original: str, bold: bool, italic: bool, text: str) -> Path | None:
    devanagari = bool(re.search(r"[\u0900-\u097f]", text))
    candidates = _font_catalog()
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda path: _font_score(path, original, bold, italic, devanagari), reverse=True)
    best = ranked[0]
    return best if _font_score(best, original, bold, italic, devanagari) > 0 else None


def _text_width(text: str, size: float, font_file: Path | None) -> float:
    try:
        return float(fitz.get_text_length(text, fontfile=str(font_file) if font_file else None, fontsize=size))
    except Exception:
        return max(1.0, len(text) * size * 0.52)


def _fit_font_size(text: str, rect: fitz.Rect, requested: float, font_file: Path | None) -> float:
    lines = str(text).splitlines() or [""]
    width_limit = max(8.0, rect.width - 2.0)
    height_limit = max(8.0, rect.height - 1.0)
    size = max(5.0, min(120.0, requested))
    for _ in range(8):
        widest = max((_text_width(line, size, font_file) for line in lines), default=0.0)
        line_height = size * 1.2 * len(lines)
        if widest <= width_limit and line_height <= height_limit:
            break
        width_ratio = width_limit / max(1.0, widest)
        height_ratio = height_limit / max(1.0, line_height)
        size = max(5.0, size * min(0.95, width_ratio, height_ratio))
    return round(size, 3)


def _save_document(document: fitz.Document) -> bytes:
    """Save with structural cleanup while preserving source image quality."""
    return document.tobytes(garbage=3, clean=True, deflate=True, deflate_fonts=True, use_objstms=1)


def _signature_summary(document: fitz.Document) -> dict[str, Any]:
    """Detect signatures conservatively before any server-side rewrite."""
    try:
        flags = document.get_sigflags()
        flags = int(flags) if flags is not None else None
    except (AttributeError, TypeError, ValueError):
        flags = None
    fields: list[dict[str, Any]] = []
    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            widgets = page.widgets() or []
            for widget in widgets:
                field_type = str(getattr(widget, "field_type_string", "") or "")
                if "signature" in field_type.lower():
                    fields.append(
                        {
                            "page": page_index + 1,
                            "name": str(getattr(widget, "field_name", "") or ""),
                            "type": field_type,
                        }
                    )
    except (AttributeError, RuntimeError, TypeError):
        fields = []
    detected = bool(fields) or (flags is not None and flags not in (0, -1))
    return {"detected": detected, "flags": flags, "fields": fields, "known": flags is not None}


def _pdf_response(data: bytes, filename: str, warning: str | None = None):
    response = send_file(
        io.BytesIO(data),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename if filename.lower().endswith(".pdf") else filename + ".pdf",
    )
    response.headers["X-PDF-SHA256"] = hashlib.sha256(data).hexdigest()
    response.headers["X-PDF-Size"] = str(len(data))
    if warning:
        response.headers["X-PDF-Warning"] = warning
    return response


@app.get("/")
def index():
    return jsonify(
        success=True,
        service="pdf-editor-worker",
        version=SERVICE_VERSION,
        status="ready",
        health="/health",
        capabilities="/capabilities",
    )


@app.get("/health")
@app.get("/health/")
@app.get("/healthz")
@app.get("/api/health")
def health():
    return jsonify(success=True, service="pdf-editor-worker", version=SERVICE_VERSION)


@app.get("/capabilities")
def capabilities():
    return jsonify(
        success=True,
        service="pdf-editor-worker",
        version=SERVICE_VERSION,
        features=["inspect", "render", "replace-text", "secure-redaction", "ocr-hook", "optimized-save", "preflight", "optimize-unsigned"],
        ocrmypdf=bool(shutil.which("ocrmypdf")),
        fonts=len(_font_catalog()),
        maxUploadBytes=MAX_UPLOAD_BYTES,
        maxPages=MAX_PDF_PAGES,
    )


@app.post("/inspect-pdf")
def inspect_pdf():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    document = None
    try:
        raw, filename = _read_upload()
        document = _open_pdf(raw, request.form.get("password", ""))
        selected_pages = range(document.page_count)
        if request.form.get("page", "") != "":
            selected_pages = [_page_number(request.form.get("page"), document.page_count)]
        pages = []
        for page_index in selected_pages:
            page = document.load_page(page_index)
            spans = _text_spans(page)
            pages.append(
                {
                    "page": page_index + 1,
                    "width": round(page.rect.width, 3),
                    "height": round(page.rect.height, 3),
                    "rotation": page.rotation,
                    "fonts": _font_inventory(page),
                    "blocks": spans,
                    "text": "\n".join(span["text"] for span in spans),
                }
            )
        return jsonify(
            success=True,
            filename=filename,
            pageCount=document.page_count,
            metadata=document.metadata,
            pages=pages,
            note="Font name is reported when the PDF exposes it; embedded subset or outlined fonts may need a supplied font file.",
        )
    except PermissionError as exc:
        return _error(str(exc), 401)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 422)
    finally:
        if document:
            document.close()


@app.post("/preflight-pdf")
def preflight_pdf():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    document = None
    try:
        raw, filename = _read_upload()
        document = _open_pdf(raw, request.form.get("password", ""))
        pages = []
        font_names: set[str] = set()
        form_fields: list[dict[str, Any]] = []
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            spans = _text_spans(page)
            page_fonts = sorted({str(span.get("font", "")) for span in spans if span.get("font")})
            font_names.update(page_fonts)
            pages.append(
                {
                    "page": page_index + 1,
                    "width": round(page.rect.width, 3),
                    "height": round(page.rect.height, 3),
                    "rotation": page.rotation,
                    "textBlocks": len(spans),
                    "textCharacters": sum(len(str(span.get("text", ""))) for span in spans),
                    "fonts": page_fonts,
                }
            )
            try:
                for widget in page.widgets() or []:
                    form_fields.append(
                        {
                            "page": page_index + 1,
                            "name": str(getattr(widget, "field_name", "") or ""),
                            "type": str(getattr(widget, "field_type_string", "") or ""),
                            "value": str(getattr(widget, "field_value", "") or ""),
                        }
                    )
            except (AttributeError, RuntimeError, TypeError):
                pass
        signatures = _signature_summary(document)
        return jsonify(
            success=True,
            filename=filename,
            fileBytes=len(raw),
            pageCount=document.page_count,
            metadata=document.metadata,
            fonts=sorted(font_names),
            formFields=form_fields,
            signatures=signatures,
            pages=pages,
            safeToOptimize=not signatures["detected"] and signatures["known"],
        )
    except PermissionError as exc:
        return _error(str(exc), 401)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 422)
    finally:
        if document:
            document.close()


@app.post("/optimize-pdf")
def optimize_pdf():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    document = None
    try:
        raw, filename = _read_upload()
        document = _open_pdf(raw, request.form.get("password", ""))
        signatures = _signature_summary(document)
        if not signatures["known"]:
            raise PermissionError("Digital signature status could not be verified; optimization was refused.")
        if signatures["detected"]:
            raise PermissionError("Signed PDF optimization is refused because rewriting can invalidate the digital signature.")
        output = _save_document(document)
        response = _pdf_response(output, Path(filename).stem + "-optimized.pdf")
        response.headers["X-PDF-Original-Size"] = str(len(raw))
        response.headers["X-PDF-Optimized-Size"] = str(len(output))
        response.headers["X-PDF-Reduction-Bytes"] = str(len(raw) - len(output))
        return response
    except PermissionError as exc:
        return _error(str(exc), 409)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 422)
    finally:
        if document:
            document.close()


@app.post("/render-page")
def render_page():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    document = None
    try:
        raw, _filename = _read_upload()
        document = _open_pdf(raw, request.form.get("password", ""))
        index = _page_number(request.form.get("page", "0"), document.page_count)
        try:
            dpi = max(72, min(600, int(request.form.get("dpi", "150"))))
        except ValueError:
            dpi = 150
        pixmap = document.load_page(index).get_pixmap(dpi=dpi, alpha=False)
        return send_file(io.BytesIO(pixmap.tobytes("png")), mimetype="image/png", max_age=0)
    except PermissionError as exc:
        return _error(str(exc), 401)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 422)
    finally:
        if document:
            document.close()


@app.post("/replace-text")
def replace_text():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    document = None
    supplied_font: Path | None = None
    try:
        raw, filename = _read_upload()
        document = _open_pdf(raw, request.form.get("password", ""))
        page = document.load_page(_page_number(request.form.get("page", "0"), document.page_count))
        rect, style = _find_match(page, request.form.get("old_text", ""), request.form.get("occurrence", 0), request.form.get("rect") or None)
        replacement = str(request.form.get("replacement", ""))
        if not replacement.strip():
            raise ValueError("replacement is required.")
        if style:
            original_font = style["fontName"]
            original_size = style["fontSize"]
            color = tuple(channel / 255 for channel in style["color"])
            bold = style["bold"]
            italic = style["italic"]
        else:
            original_font, original_size, color, bold, italic = "Noto Sans", 11.0, (0, 0, 0), False, False
        requested_size = float(request.form.get("font_size") or original_size or 11)
        font_file_value = request.files.get("font_file")
        if font_file_value and font_file_value.filename:
            suffix = Path(font_file_value.filename).suffix.lower()
            if suffix not in {".ttf", ".otf", ".ttc"}:
                raise ValueError("font_file must be TTF, OTF or TTC.")
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                font_file_value.save(temporary.name)
            supplied_font = Path(temporary.name)
        else:
            supplied_font = None
        font_file = supplied_font or _resolve_font_file(original_font, bold, italic, replacement)
        fitted_size = _fit_font_size(replacement, rect, requested_size, font_file)
        # Remove only text content. Vector artwork and images below the text
        # remain untouched so replacement does not blank a colored card.
        page.add_redact_annot(rect, fill=None)
        page.apply_redactions(
            images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
            graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
            text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
        )
        kwargs = {
            "rect": rect,
            "buffer": replacement,
            "fontsize": fitted_size,
            "color": color,
            "align": 0,
            "lineheight": 1.2,
        }
        if font_file:
            kwargs["fontfile"] = str(font_file)
        else:
            kwargs["fontname"] = "hebo" if bold else "helv"
        inserted = page.insert_textbox(**kwargs)
        if inserted < 0:
            raise ValueError("Replacement did not fit in the original text box.")
        output = _save_document(document)
        output_name = Path(filename).stem + "-edited.pdf"
        warning = None if font_file else "Original font file was not available; PDF Base-14 fallback used."
        return _pdf_response(output, output_name, warning)
    except PermissionError as exc:
        return _error(str(exc), 401)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 422)
    finally:
        if supplied_font:
            try:
                supplied_font.unlink(missing_ok=True)
            except OSError:
                pass
        if document:
            document.close()


@app.post("/redact-text")
def redact_text():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    document = None
    try:
        raw, filename = _read_upload()
        document = _open_pdf(raw, request.form.get("password", ""))
        page = document.load_page(_page_number(request.form.get("page", "0"), document.page_count))
        rect, _style = _find_match(page, request.form.get("text", ""), request.form.get("occurrence", 0), request.form.get("rect") or None)
        mode = request.form.get("mode", "area").lower()
        if mode not in {"text", "area"}:
            raise ValueError("mode must be text or area.")
        page.add_redact_annot(rect, fill=None)
        if mode == "text":
            images = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
            graphics = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
        else:
            images = getattr(fitz, "PDF_REDACT_IMAGE_PIXELS", 2)
            graphics = getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1)
        page.apply_redactions(
            images=images,
            graphics=graphics,
            text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
        )
        output = _save_document(document)
        return _pdf_response(output, Path(filename).stem + "-redacted.pdf", "Area mode removes all covered page content for stronger confidentiality.")
    except PermissionError as exc:
        return _error(str(exc), 401)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 422)
    finally:
        if document:
            document.close()


@app.post("/ocr-pdf")
def ocr_pdf():
    auth_error = require_auth()
    if auth_error is not None:
        return auth_error
    executable = shutil.which("ocrmypdf")
    if not executable:
        return _error("OCRmyPDF is not installed on this worker.", 503)
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return _error("PDF file is required.")
    raw = uploaded.read()
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        return _error("PDF upload is empty or too large.", 413 if len(raw) > MAX_UPLOAD_BYTES else 400)
    language = request.form.get("language", "eng+hin+mar")
    if not re.fullmatch(r"[a-z+_-]+", language):
        return _error("Invalid OCR language selection.")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "input.pdf"
        output = Path(directory) / "ocr.pdf"
        source.write_bytes(raw)
        command = [executable, "--skip-text", "--deskew", "--rotate-pages", "-l", language, str(source), str(output)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return _error("OCR timed out. Try a smaller PDF or fewer pages.", 504)
        if completed.returncode != 0 or not output.exists():
            message = (completed.stderr or completed.stdout or "OCR failed.").strip()[-500:]
            return _error(message, 422)
        data = output.read_bytes()
        return _pdf_response(data, Path(uploaded.filename).stem + "-ocr.pdf")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
