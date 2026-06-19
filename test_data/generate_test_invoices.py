"""
generate_test_invoices.py
==========================
Builds 4 sample invoices that simulate the real-world variety described:
  1. clean_digital_invoice.pdf   -> native text layer (Tier 1)
  2. scanned_printed_invoice.pdf -> rendered as a slightly noisy image, no text layer (Tier 2)
  3. degraded_scan_invoice.pdf   -> rotated + heavy noise + blur (Tier 3 - low OCR confidence)
  4. broken_invoice.pdf          -> missing required fields even though it reads fine (validator test)

This is purely to demonstrate/test the pipeline end-to-end without needing
real invoice files.
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT_DIR = Path(__file__).parent / "sample_invoices"
OUT_DIR.mkdir(exist_ok=True)


def _invoice_lines(variant="clean"):
    base = [
        "TAX INVOICE",
        "Sharma Traders Pvt Ltd",
        "GSTIN: 27AAACS1234F1Z5",
        "Invoice No: INV-2026-0091",
        "Invoice Date: 12-06-2026",
        "Place of Supply: Maharashtra",
        "",
        "Bill To: Krishna Hardware Stores",
        "Buyer GSTIN: 27AAACK5678G1Z2",
        "",
        "HSN 7308   Steel Brackets   Qty 50   Rate 120.00   Amount 6000.00",
        "HSN 8302   Door Hinges      Qty 200  Rate 15.00    Amount 3000.00",
        "",
        "Taxable Amount: 9000.00",
        "CGST (9%): 810.00",
        "SGST (9%): 810.00",
        "Total GST: 1620.00",
        "Total Amount: 10620.00",
    ]
    if variant == "broken":
        # deliberately drop the GSTIN and mangle the total to trigger validator flags
        base = [l for l in base if "GSTIN" not in l]
        base = [l.replace("Total Amount: 10620.00", "Total Amount: 9999.00") for l in base]
    return base


def make_clean_digital_pdf(path: Path):
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 80
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, y, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    y -= 30
    for line in _invoice_lines("clean")[1:]:
        c.drawString(72, y, line)
        y -= 18
    c.save()


def make_broken_pdf(path: Path):
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 80
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, y, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    y -= 30
    for line in _invoice_lines("broken")[1:]:
        c.drawString(72, y, line)
        y -= 18
    c.save()


def _render_text_image(lines, size=(1600, 2000), font_size=28, rotate=0,
                        noise_level=0, blur=0) -> Image.Image:
    img = Image.new("L", size, color=255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size + 4)
    except OSError:
        font = font_bold = ImageFont.load_default()

    y = 80
    for i, line in enumerate(lines):
        f = font_bold if i == 0 else font
        draw.text((80, y), line, fill=0, font=f)
        y += font_size + 18

    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor=255)

    if noise_level:
        arr = np.array(img).astype(np.int16)
        noise = np.random.randint(-noise_level, noise_level, arr.shape, dtype=np.int16)
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)

    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))

    return img.convert("RGB")


def _render_handwriting_style_image(lines, size=(1600, 2200), font_size=34) -> Image.Image:
    """
    No handwriting fonts are installed in this environment, so this simulates
    the OCR-confidence effect of real handwriting (uneven baselines, character
    jitter, variable spacing) by perturbing each character's position/rotation
    individually rather than rendering clean fixed-position text. This reliably
    drops Tesseract's confidence the way real cursive/messy handwriting does -
    enough to exercise the Tier-3 / Vision-AI routing path.
    """
    img = Image.new("L", size, color=255)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    rng = np.random.default_rng(42)
    y = 80
    for line in lines:
        x = 80
        for ch in line:
            char_img = Image.new("L", (font_size + 10, font_size + 10), color=255)
            cdraw = ImageDraw.Draw(char_img)
            cdraw.text((2, 2), ch, fill=0, font=font)
            angle = rng.uniform(-18, 18)
            char_img = char_img.rotate(angle, expand=False, fillcolor=255)
            dx, dy = rng.integers(-4, 5), rng.integers(-5, 6)
            img.paste(char_img, (x + dx, y + dy), mask=Image.eval(char_img, lambda p: 255 - p))
            x += font_size - rng.integers(2, 8)
        y += font_size + rng.integers(14, 26)

    arr = np.array(img).astype(np.int16)
    noise = rng.integers(-25, 25, arr.shape, dtype=np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(0.6))
    return img.convert("RGB")


def make_image_pdf(path: Path, image: Image.Image):
    """Embed a PIL image into a PDF with NO text layer (true scanned-style PDF)."""
    image.save(path, "PDF", resolution=300.0)


def main():
    print("Generating sample invoices...")

    p1 = OUT_DIR / "clean_digital_invoice.pdf"
    make_clean_digital_pdf(p1)
    print(f"  {p1.name}  -> native text layer, should hit Tier 1")

    p2 = OUT_DIR / "scanned_printed_invoice.pdf"
    img2 = _render_text_image(_invoice_lines("clean"), noise_level=12, blur=0.4)
    make_image_pdf(p2, img2)
    print(f"  {p2.name}  -> clean-ish scan, should hit Tier 2 (OCR, high confidence)")

    p3 = OUT_DIR / "degraded_scan_invoice.pdf"
    img3 = _render_text_image(_invoice_lines("clean"), rotate=11, noise_level=85, blur=2.2)
    make_image_pdf(p3, img3)
    print(f"  {p3.name}  -> heavily rotated/noisy/blurred, should hit Tier 3 (low confidence -> flagged)")

    p4 = OUT_DIR / "broken_invoice.pdf"
    make_broken_pdf(p4)
    print(f"  {p4.name}  -> clean text but missing GSTIN + bad total, tests validator")

    p5 = OUT_DIR / "handwritten_style_invoice.pdf"
    img5 = _render_handwriting_style_image(_invoice_lines("clean"))
    make_image_pdf(p5, img5)
    print(f"  {p5.name}  -> simulated handwriting jitter, should hit Tier 3 (-> Vision AI)")

    print(f"\nDone. Files in {OUT_DIR}")


if __name__ == "__main__":
    main()
