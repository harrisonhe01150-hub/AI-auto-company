# whatsapp-core — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong) — extracted 2026-05-27

Meta WhatsApp Cloud API client. Wraps text/image/document send, media
download, and voice transcription (via OpenAI Whisper). Drop-in replacement
for the function-level helpers that lived in `src/messaging.py`.

---

## Public API

```python
wa.send_text(to, text)                                -> dict
wa.send_image_url(to, image_url, caption="")          -> dict
wa.send_document(to, doc_bytes, filename, caption="") -> dict
wa.download_media_url(media_id)                       -> str | None
wa.download_image(media_id)                           -> str | None  (base64 data URI)
wa.download_document(media_id)                        -> bytes | None
wa.transcribe_voice(media_id)                         -> str | None
```

All methods return a dict (for sends) or the requested data (for downloads).
Failures return `{"error": "..."}` or `None` without raising — the host can
log and react.

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `whatsapp_token`     | str | **yes** | — | Meta Cloud API Bearer token |
| `phone_number_id`    | str | **yes** | — | The business number's Meta phone ID |
| `openai_api_key`     | str | no      | "" | Required only if you call `transcribe_voice()` |
| `graph_api_version`  | str | no      | `"v19.0"` | Meta Graph API version |
| `default_timeout`    | int | no      | `10` | seconds for non-upload calls |

Values starting with `$NAME` are env-expanded at init time. Recommended
pattern: keep raw tokens out of JSON, put them in env vars and reference as
`$WHATSAPP_TOKEN`.

---

## Dependencies

- Python: `requests`, `openai` (only if transcribing voice)
- Other modules: none
- External services: Meta WhatsApp Cloud API, optionally OpenAI Whisper

---

## Backward-compat shim

`src/messaging.py` in the Lifong repo is now a thin shim that constructs a
Lifong-configured singleton and re-exports the original function names
(`send_whatsapp_message`, `send_meta_image_url`, `download_meta_image`,
`download_meta_document`, `transcribe_voice_message`, `get_product_image_url`)
so all existing callers (webhook_router, customer_routing, agent_d_server,
stock_commands, etc.) keep working without edits.

`get_product_image_url` is a Lifong-specific catalogue lookup — it stays in
the shim and is NOT part of the whatsapp-core module (catalogue lookup
belongs in image-sender or sales-brain).
