# ============================================================
# Этап 2А, тесты: обнаружение дубликатов
# ============================================================

import inspect_zip
import normalize_ids_fields as norm


def _load(zip_path, sample=100):
    target, _members = inspect_zip.largest_csv_member(zip_path)
    from zipfile import ZipFile

    with ZipFile(zip_path) as zf:
        with zf.open(target) as f:
            raw = f.read()
    structure = norm.decode_and_rows(raw)
    columns = inspect_zip.analyze_columns(structure["rows"][:sample])
    roles = inspect_zip.infer_roles(columns, structure["rows"][:sample])
    return structure["rows"], roles


def test_exact_and_conflicting_duplicates_detected(zip_with_duplicates):
    rows, roles = _load(zip_with_duplicates)
    scan = inspect_zip.duplicate_conflict_scan(rows, roles)
    assert scan["duplicate_ts_groups"] == 2
    assert scan["conflicting_groups"] == 1


def test_normalizer_drops_duplicates_and_counts_conflicts(zip_with_duplicates):
    rows, roles = _load(zip_with_duplicates)
    result = norm.normalize_rows(rows, roles, 2025)
    # 10 строк, из них 2 дубля ключа (1 точный + 1 конфликтный)
    assert result["duplicates_dropped"] == 2
    assert result["conflicting_duplicates"] == 1
    assert result["valid_count"] == 8


def test_clean_fixture_has_no_duplicates(zip_doc_order):
    rows, roles = _load(zip_doc_order)
    scan = inspect_zip.duplicate_conflict_scan(rows, roles)
    assert scan["duplicate_ts_groups"] == 0
    assert scan["conflicting_groups"] == 0
    result = norm.normalize_rows(rows, roles, 2025)
    assert result["duplicates_dropped"] == 0
    assert result["conflicting_duplicates"] == 0


def test_conflicting_duplicate_means_failed_flag(make_zip_file):
    import conftest as cf

    rows = cf.make_rows(5)
    conflict = list(rows[1])
    conflict[6] = "99"  # тот же ts, другой объём (OHLC-согласованно)
    path = make_zip_file("conflict.zip", cf.zip_bytes(rows + [conflict]))
    rows2, roles = _load(path)
    result = norm.normalize_rows(rows2, roles, 2025)
    assert result["conflicting_duplicates"] >= 1


def test_malformed_price_row_rejected_by_validation(make_zip_file):
    """Строка с нарушением OHLC отбраковывается на нормализации (план 9.2)."""
    import conftest as cf

    rows = cf.make_rows(5)
    bad = list(rows[2])
    bad[4] = "1.00"  # high < max(open, close)
    path = make_zip_file("malformed.zip", cf.zip_bytes(rows + [bad]))
    rows2, roles = _load(path)
    result = norm.normalize_rows(rows2, roles, 2025)
    assert result["rejected_count"] == 1
    assert result["rejected_reasons"].get("ohlc_validation") == 1
    assert result["valid_count"] == 5
