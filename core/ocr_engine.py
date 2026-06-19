"""
ocr_engine.py
=============
Tiered document-reading engine. Tries the cheapest/most-reliable method first
and only escalates when it has to.

TIER 1  - Digital text PDFs (typed/native).        Method: pdfplumber
TIER 2  - Scanned but printed text.                 Method: render page -> image -> Tesseract
TIER 3  - Handwritten / poor scan / low confidence. Method: render page -> image -> flag for Vision AI

Every page returns a PageResult with:
  - raw_text          (best text we could get, may be empty)
  - method_used        ("digital_text" | "ocr_printed" | "needs_vision_ai")
  - confidence          (0-100, our best estimate of how trustworthy raw_text is)
  - image (PIL.Image)   (only populated when we had to rasterize the page -
                          this is what gets sent to Gemini Vision for handwriting/messy formats)
"""

import io
import statistics
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF
import pdfplumber
import pytesseract
from PIL import Image

# Below this, we don't trust pdfplumber's text even if it found some
# (covers PDFs with a tiny bit of embedded text/metadata but mostly an image)
MIN_DIGITAL_CHARS = 25

# Tesseract word-confidence below this -> treat the page as likely handwritten
# or too degraded for OCR, and escalate to Vision AI instead of trusting OCR text.
TESSERACT_TRUST_THRESHOLD = 65

# Render scale for rasterizing PDF pages to images (higher = better OCR, slower)
RENDER_DPI = 300


@dataclass
class PageResult:
    page_number: int
    raw_text: str
    method_used: str
    confidence: float
    image: Optional[Image.Image] = field(default=None, repr=False)
    notes: list = field(default_factory=list)


def _pdf_page_to_image(page: "fitz.Page") -> Image.Image:
    zoom = RENDER_DPI / 72
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def _run_tesseract_with_confidence(image: Image.Image) -> tuple[str, float]:
    """
    Returns (text, mean_word_confidence 0-100).
    Reconstructs line breaks from Tesseract's block/paragraph/line numbers
    instead of flattening everything into one long string - this matters
    both for the field-position regex parser used in demo mode and for
    giving Gemini better layout context in production.
    """
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    n = len(data["text"])
    lines: dict[tuple, list[str]] = {}
    confidences = []

    for i in range(n):
        word = data["text"][i].strip()
        conf_raw = data["conf"][i]
        conf = int(conf_raw) if str(conf_raw).lstrip("-").isdigit() else -1
        if not word or conf < 0:
            continue
        confidences.append(conf)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)

    text = "\n".join(" ".join(words) for words in lines.values())
    mean_conf = statistics.mean(confidences) if confidences else 0.0
    return text, mean_conf


def read_pdf(pdf_path: str) -> list[PageResult]:
    """
    Main entry point. Reads every page of a PDF and decides, page by page,
    which tier was needed. Mixed documents (page 1 typed, page 2 a handwritten
    note) are handled correctly because the decision is per-page.
    """
    results: list[PageResult] = []

    with pdfplumber.open(pdf_path) as plumber_pdf:
        fitz_doc = fitz.open(pdf_path)

        for i, plumber_page in enumerate(plumber_pdf.pages):
            digital_text = (plumber_page.extract_text() or "").strip()

            # --- TIER 1: usable embedded digital text ---
            if len(digital_text) >= MIN_DIGITAL_CHARS:
                results.append(PageResult(
                    page_number=i + 1,
                    raw_text=digital_text,
                    method_used="digital_text",
                    confidence=99.0,
                    notes=["Native PDF text layer used directly."],
                ))
                continue

            # No reliable digital text -> rasterize the page and look closer
            fitz_page = fitz_doc[i]
            page_image = _pdf_page_to_image(fitz_page)
            ocr_text, ocr_conf = _run_tesseract_with_confidence(page_image)

            # --- TIER 2: printed text, just scanned ---
            if ocr_conf >= TESSERACT_TRUST_THRESHOLD and len(ocr_text) >= MIN_DIGITAL_CHARS:
                results.append(PageResult(
                    page_number=i + 1,
                    raw_text=ocr_text,
                    method_used="ocr_printed",
                    confidence=ocr_conf,
                    image=page_image,
                    notes=[f"Tesseract OCR, mean word confidence {ocr_conf:.1f}."],
                ))
                continue

            # --- TIER 3: low-confidence OCR -> likely handwritten, stamped,
            # rotated, low-quality scan, or a non-standard layout. Don't trust
            # Tesseract's text as ground truth; hand the image to Vision AI
            # instead and flag the page so a human can be looped in if needed.
            results.append(PageResult(
                page_number=i + 1,
                raw_text=ocr_text,  # kept only as a weak hint, not relied upon
                method_used="needs_vision_ai",
                confidence=ocr_conf,
                image=page_image,
                notes=[
                    f"Tesseract confidence too low ({ocr_conf:.1f}) or too little text "
                    f"recognized — likely handwritten, stamped, rotated, or a poor scan. "
                    f"Routed to Vision AI extraction and flagged for review."
                ],
            ))

        fitz_doc.close()

    return results
