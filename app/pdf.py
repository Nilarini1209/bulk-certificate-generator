"""Certificate rendering. One predefined template, drawn with ReportLab."""
from dataclasses import dataclass
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class CertificateData:
    certificate_id: str
    recipient_name: str
    course_name: str
    title: str
    issued_on: date
    issuer: str | None = None


def _fit_font_size(text: str, font: str, max_size: float, max_width: float) -> float:
    """Shrink font so long names/course titles stay inside the border."""
    size = max_size
    while size > 10 and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def render_certificate_pdf(data: CertificateData) -> bytes:
    buf = BytesIO()
    width, height = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setTitle(f"{data.title} - {data.recipient_name}")
    cx = width / 2
    usable = width - 160

    # Double border
    c.setStrokeColor(colors.HexColor("#1F3A5F"))
    c.setLineWidth(4)
    c.rect(25, 25, width - 50, height - 50)
    c.setLineWidth(1)
    c.rect(35, 35, width - 70, height - 70)

    c.setFillColor(colors.HexColor("#1F3A5F"))
    c.setFont("Helvetica-Bold", _fit_font_size(data.title, "Helvetica-Bold", 38, usable))
    c.drawCentredString(cx, height - 120, data.title)

    c.setFillColor(colors.black)
    c.setFont("Helvetica", 16)
    c.drawCentredString(cx, height - 175, "This is to certify that")

    c.setFillColor(colors.HexColor("#B8860B"))
    name_font = "Helvetica-BoldOblique"
    c.setFont(name_font, _fit_font_size(data.recipient_name, name_font, 40, usable))
    c.drawCentredString(cx, height - 235, data.recipient_name)
    c.setStrokeColor(colors.HexColor("#B8860B"))
    c.line(cx - 220, height - 247, cx + 220, height - 247)

    c.setFillColor(colors.black)
    c.setFont("Helvetica", 16)
    c.drawCentredString(cx, height - 290, "has successfully completed")

    c.setFont("Helvetica-Bold", _fit_font_size(data.course_name, "Helvetica-Bold", 26, usable))
    c.drawCentredString(cx, height - 335, data.course_name)

    c.setFont("Helvetica", 13)
    c.drawCentredString(cx, height - 385, f"Issued on {data.issued_on.strftime('%d %B %Y')}")

    if data.issuer:
        c.setFont("Helvetica-Oblique", 14)
        c.drawCentredString(cx, 95, data.issuer)

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawCentredString(cx, 55, f"Certificate ID: {data.certificate_id}")

    c.showPage()
    c.save()
    return buf.getvalue()
