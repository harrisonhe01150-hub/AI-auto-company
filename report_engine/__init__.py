"""
report_engine module — public init() factory.

Minimal core (v0.1) provides:
  - PDF rendering from text/HTML
  - WhatsApp delivery (via injected sender)
  - APScheduler-based cron scheduling

Lifong's complex PDF layouts (sales report, weekly summary, restock
recommendation) live in src/agent_d_report.py as a Lifong-specific reference
implementation — too coupled to layouts to extract into the module on day 1.

Future expansion: when a new client needs reports, build their templates
inside this module (likely as Jinja2 templates + ReportLab rendering).

Usage:
    from modules.report_engine import init as init_reports

    reports = init_reports({
        "timezone": "Africa/Johannesburg",
        "whatsapp_sender": send_text,
        "document_sender": send_document,
    })

    pdf = reports.simple_pdf_from_text("Daily Summary", "Total sales: R12,500")
    reports.send_pdf_to(recipient_waid, pdf, filename="daily.pdf", caption="📊")
    reports.schedule_daily("18:00", my_daily_handler)
"""
from .module import init, ReportEngine, __version__

__all__ = ["init", "ReportEngine", "__version__"]
