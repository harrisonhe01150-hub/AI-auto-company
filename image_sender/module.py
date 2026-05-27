"""
image_sender.module — Detect SKU codes in a piece of text, look up each one's
product image URL from a catalogue, and dispatch the images via an injected
send callable.

Originally lived as `_find_all_skus_in_text` + `send_meta_image_url` +
`get_product_image_url` scattered across webhook_router.py + messaging.py.
Extracted into the warehouse on 2026-05-27.

Public API (returned by init):
    find_skus_in_text(text)                     -> list[(sku, image_url)]
    send_for_skus(to, skus, max_images=None)    -> dict   {sent, failed, total}
    send_for_text(to, text)                     -> dict   (convenience: detect + send)
"""
import json
import logging
import os
import re
import threading
from pathlib import Path

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


def _default_lifong_skus_loader(catalog_path: Path, image_field: str, fallback_field: str) -> list:
    """Default loader for Lifong stock.json (nested categories).
    Returns list of (SKU_UPPER, image_url) tuples for SKUs that have a URL."""
    if not catalog_path.exists():
        return []
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Cannot load stock catalog at {catalog_path}: {e}")
        return []
    out = []
    skip = {"business_rules", "categories", "escalation_policy"}
    for cat, items in data.items():
        if cat in skip or not isinstance(items, list):
            continue
        for item in items:
            sku = (item.get("sku") or "").upper()
            url = item.get(image_field) or item.get(fallback_field)
            if sku and url:
                out.append((sku, url))
    return out


class ImageSender:
    """A configured image-sender for a single client."""

    def __init__(self, config: dict):
        if "send_callable" not in config:
            raise ValueError("image_sender: missing required config: 'send_callable'")
        self._send = config["send_callable"]
        if not callable(self._send):
            raise ValueError("image_sender: send_callable must be callable (to, url, caption) -> dict")

        self.max_images_per_msg = int(config.get("max_images_per_msg", 4))

        # Catalogue access: either inject a loader callable OR a JSON path
        loader = config.get("skus_loader")
        if loader is None:
            catalog_path = config.get("stock_catalog_path")
            if not catalog_path:
                raise ValueError(
                    "image_sender: provide either `skus_loader` callable "
                    "OR `stock_catalog_path` so the module can find image URLs"
                )
            catalog_path = Path(catalog_path)
            image_field    = config.get("image_url_field", "image_url")
            fallback_field = config.get("fallback_field",  "image")
            self._loader = lambda: _default_lifong_skus_loader(
                catalog_path, image_field, fallback_field
            )
        elif callable(loader):
            self._loader = loader
        else:
            raise ValueError("skus_loader must be callable")

    def find_skus_in_text(self, text: str) -> list:
        """Find all SKU codes from the catalogue that appear in `text`.
        Returns list of (sku, image_url) tuples in detection order, deduped."""
        if not text:
            return []
        all_skus = self._loader()  # [(SKU, url), ...]
        found = []
        seen = set()
        try:
            norm = re.sub(r"\s+", " ", text.upper())
            norm_nohyphen = norm.replace("-", "").replace("#", "")
            for sku, url in all_skus:
                if not url or sku in seen:
                    continue
                # Exact-match-with-word-boundary first
                if re.search(r"(?<![A-Z0-9])" + re.escape(sku) + r"(?![A-Z0-9])", norm):
                    found.append((sku, url))
                    seen.add(sku)
                    continue
                # Hyphen-tolerant fallback: NC-01 vs NC01
                sku_nh = sku.replace("-", "").replace("#", "")
                if len(sku_nh) >= 3 and re.search(
                    r"(?<![A-Z0-9])" + re.escape(sku_nh) + r"(?![A-Z0-9])",
                    norm_nohyphen,
                ):
                    found.append((sku, url))
                    seen.add(sku)
        except Exception as e:
            logger.warning(f"SKU image lookup failed: {e}")
        return found

    def send_for_skus(self, to: str, skus: list, max_images: int = None,
                       caption: str = "") -> dict:
        """Send up to N images for the given SKU list.
        `skus` is a list of (sku, url) tuples (from find_skus_in_text).
        Each send is dispatched in a background thread."""
        if max_images is None:
            max_images = self.max_images_per_msg
        if not skus:
            return {"sent": 0, "failed": 0, "total": 0, "queued": 0}

        queued = 0
        for sku, url in skus[:max_images]:
            if not url:
                continue
            try:
                threading.Thread(
                    target=self._send,
                    args=(to, url, caption),
                    daemon=True,
                ).start()
                queued += 1
                logger.info(f"Queued image send for SKU {sku} -> {to} (url={url[:60]})")
            except Exception as e:
                logger.error(f"Failed to queue image send for {sku}: {e}")

        # Note: sends are async — actual sent/failed counts aren't known here.
        # We return queued count; callers should rely on logs for delivery.
        return {"sent": 0, "failed": 0, "total": len(skus), "queued": queued}

    def send_for_text(self, to: str, text: str, max_images: int = None) -> dict:
        """Convenience: detect SKUs in `text` and dispatch images for them."""
        skus = self.find_skus_in_text(text)
        return self.send_for_skus(to, skus, max_images=max_images)


def init(config: dict) -> ImageSender:
    config = _expand_env(config)
    return ImageSender(config)
