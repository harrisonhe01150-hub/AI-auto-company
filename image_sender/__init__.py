"""
image_sender module — public init() factory.

Usage:
    from modules.image_sender import init as init_imgs

    imgs = init_imgs({
        "stock_catalog_path":  "inventory/stock.json",
        "image_url_field":     "image_url",
        "fallback_field":      "image",
        "max_images_per_msg":  4,
        "send_callable":       send_meta_image_url,   # injected
    })

    skus = imgs.find_skus_in_text("Got it for NC02 and D1!")
    sent = imgs.send_for_skus(to="27...", skus=skus)
"""
from .module import init, ImageSender, __version__

__all__ = ["init", "ImageSender", "__version__"]
