# ============================================================
# Этап 2А, тесты: обработка неверного или повреждённого архива
# ============================================================

from zipfile import ZipFile

import inspect_zip


def test_truncated_zip_fails_cleanly(make_zip_file, zip_doc_order):
    good = zip_doc_order.read_bytes()
    truncated = make_zip_file("truncated.zip", good[: len(good) // 2])
    facts = inspect_zip.zip_facts(truncated)
    # Либо не открывается как ZIP, либо CRC не проходит — но без исключения
    assert facts.get("is_zip") is False or facts.get("crc_ok") is False


def test_garbage_bytes_fails_cleanly(make_zip_file):
    garbage = make_zip_file("garbage.zip", b"this is not a zip archive at all")
    facts = inspect_zip.zip_facts(garbage)
    assert facts["is_zip"] is False


def test_bad_crc_member_detected(zip_bad_crc):
    facts = inspect_zip.zip_facts(zip_bad_crc)
    assert facts["is_zip"] is True
    assert facts["crc_ok"] is False
    assert facts["bad_member"] == "candles.csv"


def test_inspect_on_bad_crc_reports_error(zip_bad_crc):
    report = inspect_zip.inspect(zip_bad_crc, 2025, sample=50)
    # Ошибка фиксируется структурно, без падения процесса
    assert (
        report.get("error")
        or report.get("zip_facts", {}).get("crc_ok") is False
    )


def test_empty_member_reports_no_data(zip_empty):
    report = inspect_zip.inspect(zip_empty, 2025, sample=50)
    assert "error" in report
    assert "пустой архив" in report["error"]


def test_missing_file_reports_cleanly(tmp_path):
    report = inspect_zip.inspect(tmp_path / "nope.zip", 2025, sample=50)
    assert "error" in report


def test_fetch_verify_rejects_non_zip(tmp_path):
    import fetch_archive_zip as fetch

    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    result = fetch.verify_zip_integrity(junk)
    assert result["is_zip"] is False
    assert result["crc_ok"] is False


def test_fetch_meta_written_even_without_token(tmp_path, monkeypatch):
    import hal_config as cfg
    import hal_common as common

    monkeypatch.setattr(cfg, "RAW_ZIPS_DIR", tmp_path / "raw")
    monkeypatch.setattr(cfg, "HTTP_META_DIR", tmp_path / "meta")
    monkeypatch.delenv("TINKOFF_READONLY_TOKEN", raising=False)
    import fetch_archive_zip as fetch

    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is False
    assert meta["token_available"] is False
    assert meta["reason_not_executed"]
    assert "HTTP" not in (meta.get("reason_not_executed") or "")
