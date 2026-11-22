# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А: фикстуры тестов прототипа.
#
# Все фиксёры СИНТЕТИЧЕСКИЕ: строки формируются в документированном
# порядке полей (UID, UTC, open, close, high, low, volume) — это
# тестовые данные, а НЕ фактический архив. Содержимое реального
# архива прототип НЕ предполагает.
# ============================================================

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

HAL_DIR = Path(__file__).resolve().parent.parent
if str(HAL_DIR) not in sys.path:
    sys.path.insert(0, str(HAL_DIR))

UID = "e6123145-9665-43e0-8413-cd61b8aa9b13"
FIGI = "BBG004730N88"

HEADER = ["UID", "UTC", "open", "close", "high", "low", "volume"]


def make_rows(n_minutes=10, base_price="260.00", start_day="2025-01-03",
              start_hhmm="07:00", uid=UID):
    """Строки в документированном порядке (o, c, h, l; h=max(o,c)+0.02, l=min-0.02)."""
    from decimal import Decimal

    rows = []
    base_h, base_m = (int(x) for x in start_hhmm.split(":"))
    price = Decimal(base_price)
    for i in range(n_minutes):
        hh = base_h + (base_m + i) // 60
        mm = (base_m + i) % 60
        ts = f"{start_day}T{hh:02d}:{mm:02d}:00Z"
        o = price
        c = price + Decimal("0.01")
        h = max(o, c) + Decimal("0.02")
        l = min(o, c) - Decimal("0.02")
        rows.append([uid, ts, str(o), str(c), str(h), str(l), str(100 + i)])
        price = c
    return rows


def zip_bytes(rows, header=None, delimiter=",", encoding="utf-8",
              compress_type=zipfile.ZIP_DEFLATED, member_name="candles.csv"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress_type) as zf:
        body = io.StringIO()
        if header is not None:
            body.write(delimiter.join(header) + "\n")
        for row in rows:
            body.write(delimiter.join(str(x) for x in row) + "\n")
        zf.writestr(member_name, body.getvalue().encode(encoding))
    return buf.getvalue()


def write_zip(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


@pytest.fixture()
def hal_paths():
    import importlib

    import hal_config as cfg

    importlib.reload(cfg)
    return cfg


@pytest.fixture()
def make_zip_file(tmp_path):
    def _make(name, data):
        return write_zip(tmp_path / name, data)
    return _make


@pytest.fixture()
def zip_doc_order(make_zip_file):
    """Без заголовка — допущение о наличии заголовка в архиве не вводится."""
    return make_zip_file("doc_order.zip", zip_bytes(make_rows(10)))


@pytest.fixture()
def zip_doc_order_header(make_zip_file):
    return make_zip_file(
        "doc_order_header.zip", zip_bytes(make_rows(10), header=HEADER)
    )


@pytest.fixture()
def zip_with_duplicates(make_zip_file):
    rows = make_rows(8)
    exact_dup = list(rows[3])          # тот же ts, те же значения
    conflict_dup = list(rows[5])       # тот же ts, другой объём (OHLC-согласованно)
    conflict_dup[6] = "7777"
    return make_zip_file("dups.zip", zip_bytes(rows + [exact_dup, conflict_dup]))


@pytest.fixture()
def zip_out_of_year(make_zip_file):
    rows = make_rows(6)
    bad = list(rows[0])
    bad[1] = "2024-12-31T23:59:00Z"
    return make_zip_file("out_of_year.zip", zip_bytes(rows + [bad]))


@pytest.fixture()
def zip_bad_prices(make_zip_file):
    rows = make_rows(6)
    bad = list(rows[2])
    bad[4] = "1.00"                    # high < max(open, close)
    return make_zip_file("bad_prices.zip", zip_bytes(rows + [bad]))


@pytest.fixture()
def zip_two_members(make_zip_file):
    rows = make_rows(6)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        body = io.StringIO()
        for row in rows:
            body.write(",".join(str(x) for x in row) + "\n")
        zf.writestr("candles.csv", body.getvalue().encode("utf-8"))
        zf.writestr("README.txt", "synthetic fixture")
    return make_zip_file("two_members.zip", buf.getvalue())


@pytest.fixture()
def zip_with_bom(make_zip_file):
    return make_zip_file(
        "with_bom.zip", zip_bytes(make_rows(8), encoding="utf-8-sig")
    )


@pytest.fixture()
def zip_semicolon(make_zip_file):
    return make_zip_file(
        "semicolon.zip", zip_bytes(make_rows(8), delimiter=";")
    )


@pytest.fixture()
def zip_unsorted(make_zip_file):
    rows = make_rows(8)
    shuffled = [rows[5], rows[0], rows[7], rows[2], rows[1],
                rows[6], rows[3], rows[4]]
    return make_zip_file("unsorted.zip", zip_bytes(shuffled))


@pytest.fixture()
def zip_empty(make_zip_file):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("candles.csv", "")
    return make_zip_file("empty.zip", buf.getvalue())


@pytest.fixture()
def zip_bad_crc(make_zip_file):
    """ZIP сSTORED-членом, байты которого повреждены (CRC не совпадёт)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        body = io.StringIO()
        for row in make_rows(8):
            body.write(",".join(str(x) for x in row) + "\n")
        zf.writestr("candles.csv", body.getvalue().encode("utf-8"))
    raw = bytearray(buf.getvalue())
    # Портим байт ВНУТРИ данных STORED-члена (не в заголовках и не в каталоге):
    # данные начинаются после local header (30 байт + имя файла + extra)
    uid_bytes = b"e6123145"
    data_offset = raw.find(uid_bytes)
    assert data_offset > 0
    raw[data_offset + 2] ^= 0xFF
    return make_zip_file("bad_crc.zip", bytes(raw))


@pytest.fixture()
def zip_no_volume_column(make_zip_file):
    rows = [r[:6] for r in make_rows(6)]  # убрали volume
    return make_zip_file("no_volume.zip", zip_bytes(rows))


@pytest.fixture()
def zip_no_id_column(make_zip_file):
    rows = [r[1:] for r in make_rows(6)]  # убрали uid
    return make_zip_file("no_id.zip", zip_bytes(rows))


@pytest.fixture()
def zip_no_time_column(make_zip_file):
    rows = [r[:1] + r[2:] for r in make_rows(6)]  # убрали utc
    return make_zip_file("no_time.zip", zip_bytes(rows))
