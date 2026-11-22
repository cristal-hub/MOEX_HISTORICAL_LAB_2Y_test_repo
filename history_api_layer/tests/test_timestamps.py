# ============================================================
# Этап 2А, тесты: проверка временных меток
# ============================================================

from datetime import datetime

import hal_common as common
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


def test_unsorted_input_produces_sorted_output(zip_unsorted):
    rows, roles = _load(zip_unsorted)
    result = norm.normalize_rows(rows, roles, 2025)
    keys = [k for k, _p, _v in result["valid_rows"]]
    assert keys == sorted(keys)


def test_out_of_year_rows_rejected(zip_out_of_year):
    rows, roles = _load(zip_out_of_year)
    result = norm.normalize_rows(rows, roles, 2025)
    assert result["out_of_year"] == 1
    assert result["rejected_count"] == 1
    facts = inspect_zip.time_facts(rows, roles, 2025)
    assert facts["out_of_year"] == 1


def test_minute_alignment_check(zip_doc_order):
    rows, roles = _load(zip_doc_order)
    facts = inspect_zip.time_facts(rows, roles, 2025)
    assert facts["second_zero_aligned"] == len(rows)
    assert facts["second_nonzero"] == 0


def test_nonzero_seconds_detected(make_zip_file):
    import conftest as cf

    rows = cf.make_rows(5)
    odd = list(rows[2])
    odd[1] = odd[1].replace(":00Z", ":30Z")
    path = make_zip_file("odd_seconds.zip", cf.zip_bytes(rows + [odd]))
    rows2, roles = _load(path)
    facts = inspect_zip.time_facts(rows2, roles, 2025)
    assert facts["second_nonzero"] == 1


def test_time_format_detection_variants():
    cases = {
        "2025-01-03T07:00:00Z": "%Y-%m-%dT%H:%M:%SZ",
        "2025-01-03T07:00:00+00:00": "iso8601_offset",
        "2025-01-03T07:00:00": "%Y-%m-%dT%H:%M:%S",
        "2025-01-03 07:00:00": "%Y-%m-%d %H:%M:%S",
        "20250103070000": "%Y%m%d%H%M%S",
        "1735878000": "epoch_seconds",
        "1735878000000": "epoch_millis",
    }
    for text, expected in cases.items():
        dt, label = common.parse_time_any(text)
        assert dt is not None, text
        assert label == expected, (text, label)


def test_epoch_millis_matches_expected_instant():
    from datetime import timezone

    expected = datetime.fromtimestamp(1735878000, tz=timezone.utc).replace(tzinfo=None)
    dt, label = common.parse_time_any("1735878000000")
    assert label == "epoch_millis"
    assert dt == expected


def test_invalid_time_not_parsed():
    for text in ("", "not-a-time", "2025-13-99 99:99:99", "01/02/2025 25:00"):
        dt, _label = common.parse_time_any(text)
        assert dt is None, text
