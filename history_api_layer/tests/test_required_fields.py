# ============================================================
# Этап 2А, тесты: проверка обязательных полей и ролей колонок
# ============================================================

import inspect_zip
import normalize_ids_fields as norm
from zipfile import ZipFile


def _roles_for(zip_path, sample=100):
    target, _members = inspect_zip.largest_csv_member(zip_path)
    with ZipFile(zip_path) as zf:
        with zf.open(target) as f:
            raw = f.read()
    structure = norm.decode_and_rows(raw)
    columns = inspect_zip.analyze_columns(structure["rows"][:sample])
    return structure, inspect_zip.infer_roles(columns, structure["rows"][:sample])


def test_all_roles_present(zip_doc_order):
    _structure, roles = _roles_for(zip_doc_order)
    assert "id" in roles
    assert "utc" in roles
    assert "volume" in roles
    assert len(roles["price_columns"]) == 4


def test_missing_volume_column_is_fatal_for_normalization(zip_no_volume_column):
    _structure, roles = _roles_for(zip_no_volume_column)
    assert "volume" not in roles
    rows = _structure["rows"]
    result = norm.normalize_rows(rows, roles, 2025)
    assert "fatal" in result


def test_missing_id_column_stops(zip_no_id_column):
    _structure, roles = _roles_for(zip_no_id_column)
    assert "id" not in roles
    check = norm.verify_identifier(_structure["rows"], roles)
    assert check["verified"] is False
    assert check["stop"] is True


def test_missing_time_column_is_fatal(zip_no_time_column):
    _structure, roles = _roles_for(zip_no_time_column)
    assert "utc" not in roles
    result = norm.normalize_rows(_structure["rows"], roles, 2025)
    assert "fatal" in result


def test_identifier_mismatch_stops(make_zip_file):
    import conftest as cf

    rows = cf.make_rows(4, uid="00000000-0000-0000-0000-000000000000")
    path = make_zip_file("wrong_id.zip", cf.zip_bytes(rows))
    _structure, roles = _roles_for(path)
    check = norm.verify_identifier(_structure["rows"], roles)
    assert check["verified"] is False
    assert check["mismatch_rows"] == 4
    assert check["stop"] is True


def test_identifier_figi_also_accepted(make_zip_file):
    import conftest as cf

    rows = cf.make_rows(4, uid=cf.FIGI)
    path = make_zip_file("figi_id.zip", cf.zip_bytes(rows))
    _structure, roles = _roles_for(path)
    check = norm.verify_identifier(_structure["rows"], roles)
    assert check["verified"] is True
    assert check["matches_figi"] is True


def test_canonical_fields_exposed():
    assert norm.CANONICAL_FIELDS == [
        "time", "open", "high", "low", "close", "volume", "is_complete"
    ]
