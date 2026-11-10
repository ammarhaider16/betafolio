"""Portfolio files preserve cash and remain drafts until the owner saves."""

from io import BytesIO
from datetime import datetime, timezone
import openpyxl
from betafolio.imports import parse_holdings_file


def test_excel_import_returns_quantities_and_cash_without_uploaded_prices():
    book = openpyxl.Workbook()
    book.active.append(["Ticker", "Shares", "Price", "Previous_close"])
    book.active.append(["AAPL", 10, "invalid", "invalid"])
    book.active.append(["CASH", 1500, 100, 100])
    content = BytesIO()
    book.save(content)
    draft = parse_holdings_file(content.getvalue(),'holdings.xlsx',datetime.now(timezone.utc))
    assert draft.cash == 1500 and draft.positions[0].quantity == 10
    assert draft.positions[0].price is None and draft.positions[0].previous_close is None


def test_file_import_rejects_corrupt_workbooks_and_unsupported_extensions():
    import pytest

    for filename in ("holdings.xlsx", "holdings.pdf", "holdings.xlsm"):
        with pytest.raises(ValueError):
            parse_holdings_file(b"invalid", filename, datetime.now(timezone.utc))
