# ============================================================
# Этап 2А, тесты: корректность чтения ZIP и фактической структуры
# ============================================================

from zipfile import ZipFile

import inspect_zip


def test_zip_crc_and_members(zip_doc_order):
    facts = inspect_zip.zip_facts(zip_doc_order)
    assert facts["is_zip"] is True
    assert facts["crc_ok"] is True
    assert facts["bad_member"] is None
    assert facts["members"][0]["name"] == "candles.csv"


def test_documented_order_detected_without_header(zip_doc_order):
    report = inspect_zip.inspect(zip_doc_order, 2025, sample=100, full=True)
    assert "error" not in report
    assert report["header_present"] is False
    roles = report["roles"]
    assert roles["id"] == 0
    assert roles["utc"] == 1
    assert roles["volume"] == 6
    assert roles["price_columns"] == [2, 3, 4, 5]
    assert roles["id_kind"] == "uid"
    assert roles["id_value"].startswith("e6123145")
    ohlc = roles["ohlc_mapping"]
    assert ohlc["chosen"] == "documented_open_close_high_low"
    assert ohlc["matches_documented_order"] is True


def test_header_variant_detected(zip_doc_order_header):
    report = inspect_zip.inspect(zip_doc_order_header, 2025, sample=100, full=True)
    assert report["header_present"] is True
    assert report["header_names"][0] == "UID"
    roles = report["roles"]
    assert roles["id"] == 0
    assert roles["utc"] == 1


def test_two_members_listing(zip_two_members):
    report = inspect_zip.inspect(zip_two_members, 2025, sample=100)
    assert sorted(report["all_members"]) == ["README.txt", "candles.csv"]
    assert report["target_member"] == "candles.csv"


def test_bom_detected(zip_with_bom):
    report = inspect_zip.inspect(zip_with_bom, 2025, sample=100, full=True)
    assert report["encoding"]["has_bom"] is True
    assert report["encoding"]["decodable"] is True
    assert report["roles"]["utc"] == 1


def test_delimiter_detection(zip_semicolon):
    report = inspect_zip.inspect(zip_semicolon, 2025, sample=100, full=True)
    assert report["delimiter"] == "';'"
    assert report["roles"]["utc"] == 1


def test_id_matches_requested_reported(zip_doc_order):
    report = inspect_zip.inspect(zip_doc_order, 2025, sample=100)
    matches = report["id_matches_requested"]
    assert matches["uid"] is True
    assert matches["figi"] is False


def test_largest_csv_member_selected():
    import io
    from zipfile import ZipFile

    buf = io.BytesIO()
    with ZipFile(buf, "w") as zf:
        zf.writestr("small.csv", "a,b\n1,2\n")
        zf.writestr("big.csv", "a,b\n" + "1,2\n" * 100)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.zip"
        p.write_bytes(buf.getvalue())
        target, all_members = inspect_zip.largest_csv_member(p)
        assert target.filename == "big.csv"
        assert all_members == ["small.csv", "big.csv"]
