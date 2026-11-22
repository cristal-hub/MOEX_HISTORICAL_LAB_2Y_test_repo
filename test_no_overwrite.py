# ============================================================
# Этап 2А, тесты: защита от случайной перезаписи существующих файлов
# ============================================================

import pytest

import hal_common as common


def test_atomic_write_refuses_overwrite(tmp_path):
    target = tmp_path / "out.csv"
    common.atomic_write_text(target, "first", allow_overwrite=False)
    with pytest.raises(common.NoOverwriteError):
        common.atomic_write_text(target, "second", allow_overwrite=False)
    assert target.read_text(encoding="utf-8") == "first"


def test_atomic_write_explicit_overwrite_allowed(tmp_path):
    target = tmp_path / "out.csv"
    common.atomic_write_text(target, "first", allow_overwrite=False)
    common.atomic_write_text(target, "second", allow_overwrite=True)
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_write_leaves_no_tmp(tmp_path):
    target = tmp_path / "out.json"
    common.atomic_write_json(target, {"a": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    assert target.exists()


def test_normalize_output_refuses_overwrite(zip_doc_order, tmp_path):
    """Скрипты прототипа пишут только атомарно и без перезаписи по умолчанию."""
    out = tmp_path / "normalized.csv"
    common.atomic_write_text(out, "existing content")
    import normalize_ids_fields as norm
    from decimal import Decimal

    valid_rows = [
        ("2025-01-03T07:00:00Z", {"open": Decimal("260.00"),
                                  "high": Decimal("260.03"),
                                  "low": Decimal("259.98"),
                                  "close": Decimal("260.01")}, 100),
    ]
    with pytest.raises(common.NoOverwriteError):
        norm.write_normalized_csv(out, valid_rows)


def test_fetch_refuses_existing_target(monkeypatch, tmp_path):
    """При существующем целевом ZIP запрос вообще не выполняется (fail-closed)."""
    import hal_config as cfg
    import fetch_archive_zip as fetch

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    existing = raw_dir / f"{cfg.INSTRUMENT['ticker']}_2025_uid.zip"
    existing.write_bytes(b"existing")
    monkeypatch.setattr(cfg, "RAW_ZIPS_DIR", raw_dir)
    monkeypatch.setenv("TINKOFF_READONLY_TOKEN", "dummy-token-for-test")

    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is False
    assert "NO_OVERWRITE" in (meta.get("reason_not_executed") or "")


def test_fetch_token_missing_never_calls_http(monkeypatch, tmp_path):
    import hal_config as cfg
    import fetch_archive_zip as fetch

    monkeypatch.setattr(cfg, "RAW_ZIPS_DIR", tmp_path / "raw")
    monkeypatch.delenv("TINKOFF_READONLY_TOKEN", raising=False)
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["attempts"] == []
    assert meta["executed"] is False


def test_tmp_and_rename_in_same_directory(tmp_path):
    target = tmp_path / "out.txt"
    common.atomic_write_text(target, "x")
    # Проверяем, что tmp создаётся в том же каталоге (условие атомарности)
    import inspect as inspect_mod

    src = inspect_mod.getsource(common.atomic_write_bytes)
    assert "path.parent" in src or "with_name" in src
