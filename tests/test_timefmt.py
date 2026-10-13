from datetime import datetime, timezone

from app.services.timefmt import format_kyiv, to_kyiv


def test_naive_utc_shown_as_kyiv():
    # вересень — літній час Києва (UTC+3)
    naive_utc = datetime(2026, 9, 21, 12, 0, 0)
    assert format_kyiv(naive_utc) == "21.09.2026 15:00"


def test_aware_utc_shown_as_kyiv():
    aware = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    local = to_kyiv(aware)
    assert local is not None
    assert local.hour == 15
    assert format_kyiv(aware) == "21.09.2026 15:00"


def test_empty_datetime():
    assert format_kyiv(None) == ""
