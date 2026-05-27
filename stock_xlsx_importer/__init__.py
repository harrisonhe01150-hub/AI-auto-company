"""
stock_xlsx_importer module — public init() factory.

Usage:
    from modules.stock_xlsx_importer import init as init_xlsx_importer

    importer = init_xlsx_importer({
        "stock_catalog_path": "inventory/stock.json",
        "ledger_path":        "runtime_data/stock_ledger.json",
        "timezone":           "Africa/Johannesburg",
    })

    reply_text = importer.process_xlsx_bytes(xlsx_bytes,
                                              uploader_waid="27...",
                                              filename="inventory.xlsx")
"""
from .module import init, StockXlsxImporter, __version__

__all__ = ["init", "StockXlsxImporter", "__version__"]
