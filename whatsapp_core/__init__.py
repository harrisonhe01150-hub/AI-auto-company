"""
whatsapp_core module — public init() factory.

Usage:
    from modules.whatsapp_core import init as init_wa

    wa = init_wa({
        "whatsapp_token":  "$WHATSAPP_TOKEN",
        "phone_number_id": "$PHONE_NUMBER_ID",
        "openai_api_key":  "$OPENAI_API_KEY",  # for voice transcription
        "graph_api_version": "v19.0",
    })

    wa.send_text(to, "Hello")
    wa.send_image_url(to, image_url, caption="")
    wa.send_document(to, pdf_bytes, filename="report.pdf", caption="")
    image_b64 = wa.download_image(media_id)
    doc_bytes = wa.download_document(media_id)
    text      = wa.transcribe_voice(media_id)
"""
from .module import init, WhatsAppCore, __version__

__all__ = ["init", "WhatsAppCore", "__version__"]
