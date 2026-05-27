# report-engine — Module spec

**Version**: 0.1.0 (minimal core)
**Status**: Live skeleton (Lifong) — extracted 2026-05-27

Provides PDF rendering + WhatsApp delivery + cron scheduling building blocks.

**Scope decision**: Lifong's complex sales-report / weekly / restock-rec PDFs
live in `src/agent_d_report.py` and `src/scheduler_jobs.py` — they're too
coupled to Lifong's specific data shape to extract on day 1 of the warehouse.
This module gives clients a reusable foundation; complex layouts get built
out per-client as Jinja2 + ReportLab templates added to this module over time.

---

## Public API

```python
reports.simple_pdf_from_text(title, body, footer="") -> bytes
    # One-page text PDF using ReportLab

reports.send_text_to(recipient, text)                      -> dict
reports.send_pdf_to(recipient, pdf_bytes, filename, caption="") -> dict

reports.schedule_daily(hh_mm, callback, *args, **kwargs)   -> Job
reports.schedule_cron(cron_expr, callback, *args, **kwargs) -> Job
reports.start_scheduler()
reports.shutdown_scheduler(wait=False)
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `timezone`         | str      | no | `"Africa/Johannesburg"` | Scheduler timezone |
| `whatsapp_sender`  | callable | no | None | `(to, text) -> dict` for `send_text_to` |
| `document_sender`  | callable | no | None | `(to, bytes, filename, caption) -> dict` for `send_pdf_to` |
| `pdf_font`         | str      | no | `"Helvetica"` | Default body font |
| `pdf_title_size`   | int      | no | `16` | |
| `pdf_body_size`    | int      | no | `11` | |

If a sender is not configured the corresponding `send_*` method returns
`{"error": "..."}` without raising.

---

## Dependencies

- Python: `pytz` (always), `reportlab` (for PDF), `apscheduler` (for cron)
- Other modules: typically composed with `whatsapp-core` (sender callables)
- External services: WhatsApp delivery via injected senders

---

## Integration pattern

```python
from modules.whatsapp_core import init as init_wa
from modules.report_engine  import init as init_reports

wa      = init_wa(load_cfg("whatsapp_core"))
reports = init_reports({
    "timezone":         "Africa/Johannesburg",
    "whatsapp_sender":  wa.send_text,
    "document_sender":  wa.send_document,
})

def daily_summary():
    body = build_daily_summary_text()   # client-specific
    pdf  = reports.simple_pdf_from_text("Daily Summary", body)
    reports.send_pdf_to("27...", pdf, "summary.pdf", "📊 Today's summary")

reports.schedule_daily("18:00", daily_summary)
reports.start_scheduler()
```

---

## Future expansion (when needed)

- **Jinja2 HTML templates** → ReportLab rendering for complex layouts (tables, charts)
- **CSV / xlsx export** alongside PDF
- **Multi-language report templates**
- **Chart helpers** (matplotlib → image → embed in PDF)
- **Email delivery** alongside WhatsApp
- **Report archiving** (S3 / local backup)

These all become methods on `ReportEngine` as clients ask for them.
