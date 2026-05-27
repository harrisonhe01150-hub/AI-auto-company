"""
report_engine.module — Reports + scheduling infrastructure.

Provides three core building blocks every client's reporting needs:
  1. PDF generation (text → simple PDF via ReportLab)
  2. WhatsApp delivery (text + PDF via injected senders)
  3. Cron-style scheduling (via APScheduler)

Lifong's complex sales/weekly/restock PDF layouts live in
src/agent_d_report.py (too coupled to Lifong's data shape to extract on day 1);
they can be re-implemented here as Jinja2 + ReportLab templates when a client
explicitly needs them.

Public API (returned by init):
    simple_pdf_from_text(title, body, footer="") -> bytes
    send_text_to(recipient, text)                -> dict
    send_pdf_to(recipient, pdf_bytes, filename, caption="") -> dict
    schedule_daily(hh_mm, callback, *args)      -> Job
    schedule_cron(cron_expr, callback, *args)   -> Job
    start_scheduler()                            -> None
    shutdown_scheduler()                         -> None
"""
import logging
import os
from datetime import datetime
from io import BytesIO

import pytz

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class ReportEngine:
    """Configured report-generation + delivery + scheduling for a single client."""

    def __init__(self, config: dict):
        self.tz = pytz.timezone(config.get("timezone", "Africa/Johannesburg"))

        # Injected senders (callables matching whatsapp_core's send_text / send_document signatures)
        self._send_text = config.get("whatsapp_sender")
        self._send_doc  = config.get("document_sender")

        # PDF styling defaults
        self.pdf_font  = config.get("pdf_font", "Helvetica")
        self.pdf_title_size = int(config.get("pdf_title_size", 16))
        self.pdf_body_size  = int(config.get("pdf_body_size", 11))

        # APScheduler instance — created on demand
        self._scheduler = None

    # ── PDF generation ─────────────────────────────────────────────────────
    def simple_pdf_from_text(self, title: str, body: str, footer: str = "") -> bytes:
        """Render a minimal one-page PDF: title + body + optional footer.

        For richer layouts (tables, images, charts) build Jinja2 + ReportLab
        templates and add them as methods on this class.
        """
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.units import cm
        except ImportError:
            raise RuntimeError(
                "report_engine.simple_pdf_from_text requires reportlab — "
                "add `reportlab` to requirements.txt"
            )

        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=2*cm, rightMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        story = [
            Paragraph(title, styles["Title"]),
            Spacer(1, 0.5*cm),
        ]
        # Body: split on \n and emit one paragraph per line so layout is predictable
        for line in (body or "").split("\n"):
            if line.strip():
                story.append(Paragraph(line, styles["Normal"]))
            else:
                story.append(Spacer(1, 0.2*cm))
        if footer:
            story.append(Spacer(1, 1*cm))
            story.append(Paragraph(f"<i>{footer}</i>", styles["Italic"]))
        doc.build(story)
        return buf.getvalue()

    # ── Delivery ───────────────────────────────────────────────────────────
    def send_text_to(self, recipient: str, text: str) -> dict:
        if not self._send_text:
            return {"error": "whatsapp_sender not configured"}
        return self._send_text(recipient, text)

    def send_pdf_to(self, recipient: str, pdf_bytes: bytes,
                    filename: str = "report.pdf", caption: str = "") -> dict:
        if not self._send_doc:
            return {"error": "document_sender not configured"}
        return self._send_doc(recipient, pdf_bytes, filename, caption)

    # ── Scheduling (APScheduler wrapper) ───────────────────────────────────
    def _ensure_scheduler(self):
        if self._scheduler is None:
            try:
                from apscheduler.schedulers.background import BackgroundScheduler
            except ImportError:
                raise RuntimeError(
                    "report_engine scheduling requires apscheduler — "
                    "add `apscheduler` to requirements.txt"
                )
            self._scheduler = BackgroundScheduler(timezone=str(self.tz))

    def schedule_daily(self, hh_mm: str, callback, *args, **kwargs):
        """Run `callback(*args, **kwargs)` every day at HH:MM (in module timezone)."""
        self._ensure_scheduler()
        from apscheduler.triggers.cron import CronTrigger
        h, m = hh_mm.split(":")
        trigger = CronTrigger(hour=int(h), minute=int(m), timezone=str(self.tz))
        return self._scheduler.add_job(callback, trigger=trigger, args=args, kwargs=kwargs)

    def schedule_cron(self, cron_expr: str, callback, *args, **kwargs):
        """Run `callback` on a cron expression (apscheduler 5-field syntax: minute hour day month dow)."""
        self._ensure_scheduler()
        from apscheduler.triggers.cron import CronTrigger
        return self._scheduler.add_job(
            callback,
            trigger=CronTrigger.from_crontab(cron_expr, timezone=str(self.tz)),
            args=args, kwargs=kwargs,
        )

    def start_scheduler(self):
        self._ensure_scheduler()
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown_scheduler(self, wait: bool = False):
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=wait)


def init(config: dict) -> ReportEngine:
    config = _expand_env(config)
    return ReportEngine(config)
