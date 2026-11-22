# ============================================================
# Этап 2А, тесты раунда ревью 1 (P2): отчёт пригодности 1M -> 5M
#
# Требования задания:
# 1. time_sorted проверяется по ИСХОДНОЙ последовательности ДО сортировки.
# 2. Проверяются фактические значения is_complete.
# 3. Строки is_complete=false не попадают в завершённые данные без
#    явного учёта.
# 4. Завершённость 5M-бакета = is_complete ВСЕХ входящих строк И
#    временной горизонт H_end (план 8.3.2 v2.2).
# 5. Тесты: несортированный CSV; is_complete=false; смешанные значения
#    is_complete внутри одного 5M-бакета; неполный последний бакет.
#
# Правила агрегации плана v2.2 не меняются; вход — нормализованный CSV
# канонической схемы time,open,high,low,close,volume,is_complete.
# ============================================================

from decimal import Decimal

import report_suitability_5m as suit

HEADER = "time,open,high,low,close,volume,is_complete"


def _row(day, hh, mm, i, price, complete="true"):
    o = price
    c = price + Decimal("0.01")
    h = max(o, c) + Decimal("0.02")
    l = min(o, c) - Decimal("0.02")
    return (f"2025-01-03T{hh:02d}:{mm:02d}:00", str(o), str(h), str(l),
            str(c), str(100 + i), complete)


def make_csv_rows(n_minutes, start_hh=7, start_mm=0, base=Decimal("260.00"),
                  complete_map=None):
    """Канонические строки: колонка is_complete управляется complete_map
    ({индекс_строки: 'true'/'false'/'…'}), по умолчанию все true."""
    rows = []
    price = base
    for i in range(n_minutes):
        hh = start_hh + (start_mm + i) // 60
        mm = (start_mm + i) % 60
        flag = "true"
        if complete_map is not None and i in complete_map:
            flag = complete_map[i]
        rows.append(_row("2025-01-03", hh, mm, i, price, flag))
        price = price + Decimal("0.01")
    return rows


def write_csv(path, rows):
    lines = [HEADER] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _volume_total(rows):
    return sum(int(r[5]) for r in rows)


# ---------- 1. Несортированный CSV ----------

def test_unsorted_csv_detected_before_sorting(tmp_path):
    """time_sorted=false по исходному порядку; агрегация всё равно корректна."""
    rows = make_csv_rows(10)                      # 07:00..07:09, 2 полных бакета
    shuffled = [rows[5], rows[0], rows[7], rows[2], rows[1],
                rows[6], rows[3], rows[4], rows[8], rows[9]]
    csv_path = write_csv(tmp_path / "unsorted.csv", shuffled)
    report = suit.build_report(csv_path, 2025)
    # КЛЮЧЕВАЯ проверка: исходная последовательность НЕ отсортирована
    assert report["time_sorted"] is False
    assert any("не отсортированы" in b for b in report["blockers"])
    assert report["suitable_for_5m_aggregation"] is False
    # консервация не зависит от порядка входа
    agg = report["aggregation_5m"]
    assert agg["conservation"]["A1_sum_volume_1m_eq_all_buckets"] is True
    assert agg["conservation"]["A2_minutes_eq_rows"] is True
    assert agg["volume_1m_input"] == _volume_total(rows)


def test_sorted_csv_passes_original_order_check(tmp_path):
    rows = make_csv_rows(10)
    csv_path = write_csv(tmp_path / "sorted.csv", rows)
    report = suit.build_report(csv_path, 2025)
    assert report["time_sorted"] is True
    assert report["suitable_for_5m_aggregation"] is True
    assert report["blockers"] == []


# ---------- 2. is_complete=false ----------

def test_is_complete_false_excluded_with_explicit_accounting(tmp_path):
    """Последняя строка false: её бакет исключён (contains_incomplete_row),
    объём false-строк учтён отдельно и целиком в исключённых бакетах."""
    rows = make_csv_rows(10, complete_map={9: "false"})   # 07:09 false
    csv_path = write_csv(tmp_path / "false_tail.csv", rows)
    report = suit.build_report(csv_path, 2025)
    assert report["is_complete_values"] == {"true": 9, "false": 1, "invalid": 0}
    agg = report["aggregation_5m"]
    assert agg["rows_is_complete_false"] == 1
    assert agg["volume_of_incomplete_rows"] == int(rows[9][5])
    assert agg["incomplete_rows_fully_inside_excluded_buckets"] is True
    # бакет 07:05 содержит false-строку -> исключён с нужной причиной
    excluded = {e["bucket_start_utc"]: e for e in agg["excluded_incomplete_buckets"]}
    assert "2025-01-03T07:05:00" in excluded
    assert excluded["2025-01-03T07:05:00"]["reason"] == "contains_incomplete_row"
    assert agg["complete_bucket_count"] == 1               # только 07:00
    # горизонт — только по строкам is_complete=true: 07:08 + 1 мин
    assert agg["last_complete_1m_start_utc"] == "2025-01-03T07:08:00"
    assert agg["h_end_utc"] == "2025-01-03T07:09:00"
    # строка false не попала в «завершённые» данные — блокер зафиксирован
    assert any("is_complete=false" in b for b in report["blockers"])
    assert report["suitable_for_5m_aggregation"] is False


def test_all_rows_false_no_complete_data(tmp_path):
    """Все строки false: H_end не определён, complete-бакетов нет."""
    rows = make_csv_rows(6, complete_map={i: "false" for i in range(6)})
    csv_path = write_csv(tmp_path / "all_false.csv", rows)
    report = suit.build_report(csv_path, 2025)
    agg = report["aggregation_5m"]
    assert agg["h_end_utc"] is None
    assert agg["complete_bucket_count"] == 0
    assert agg["incomplete_bucket_count"] == agg["bucket_count"]
    assert any("H_end не определён" in b for b in report["blockers"])
    # консервация уровня A всё равно сходится (все строки участвуют)
    assert agg["conservation"]["A1_sum_volume_1m_eq_all_buckets"] is True


def test_invalid_is_complete_value_blocked(tmp_path):
    rows = make_csv_rows(6, complete_map={2: "yes"})
    csv_path = write_csv(tmp_path / "invalid_ic.csv", rows)
    report = suit.build_report(csv_path, 2025)
    assert report["is_complete_values"]["invalid"] == 1
    assert report["parse_problems"]["is_complete_invalid"] == 1
    assert any("is_complete" in b for b in report["blockers"])
    assert report["suitable_for_5m_aggregation"] is False


# ---------- 3. Смешанные значения is_complete внутри одного бакета ----------

def test_mixed_is_complete_in_one_bucket(tmp_path):
    """Бакет 07:00 содержит true, false, true -> исключён целиком,
    объём и минуты этого бакета учтены в excluded (план 9.3-B)."""
    rows = make_csv_rows(3, complete_map={1: "false"})    # 07:00..07:02
    csv_path = write_csv(tmp_path / "mixed.csv", rows)
    report = suit.build_report(csv_path, 2025)
    agg = report["aggregation_5m"]
    assert agg["bucket_count"] == 1
    assert agg["complete_bucket_count"] == 0
    assert agg["incomplete_bucket_count"] == 1
    e = agg["excluded_incomplete_buckets"][0]
    assert e["reason"] == "contains_incomplete_row"
    assert e["rows_is_complete_false"] == 1
    assert e["volume"] == _volume_total(rows)
    # B1/B2: разбиение сходится, ничего не потеряно
    assert agg["conservation"]["B1_volume_split_balance"] is True
    assert agg["conservation"]["B2_minutes_split_balance"] is True
    assert agg["volume_complete_buckets"] == 0
    assert agg["volume_excluded_incomplete_buckets"] == _volume_total(rows)
    # агрегатор не «повысил» завершённость (план 8.3.7)
    assert agg["rows_is_complete_false"] == 1


# ---------- 4. Неполный последний бакет (горизонт H_end) ----------

def test_incomplete_last_bucket_by_time_horizon(tmp_path):
    """Последняя минута 07:03 (H_end=07:04): бакет [07:00,07:05) не
    закрывается по времени -> incomplete, причина tail_window_beyond_horizon
    (обязательный пример 8.3, случай 10:03)."""
    rows = make_csv_rows(4)                                # 07:00..07:03
    csv_path = write_csv(tmp_path / "tail4.csv", rows)
    report = suit.build_report(csv_path, 2025)
    agg = report["aggregation_5m"]
    assert agg["h_end_utc"] == "2025-01-03T07:04:00"
    assert agg["complete_bucket_count"] == 0
    assert agg["excluded_incomplete_buckets"][0]["reason"] == \
        "tail_window_beyond_horizon"
    # плотность 4/5 не влияет на завершённость по флагам — все строки true
    assert agg["rows_is_complete_false"] == 0
    # данные пригодны для агрегации; бакет исключён явно, без потери учёта
    assert report["suitable_for_5m_aggregation"] is True
    assert agg["conservation"]["B1_volume_split_balance"] is True


def test_last_bucket_complete_when_last_minute_closes_bucket(tmp_path):
    """Последняя минута 10:04 (H_end=10:05): бакет [10:00,10:05) —
    завершён (обязательный пример 8.3, случай 10:04)."""
    rows = make_csv_rows(5, start_hh=10)                   # 10:00..10:04
    csv_path = write_csv(tmp_path / "tail5.csv", rows)
    report = suit.build_report(csv_path, 2025)
    agg = report["aggregation_5m"]
    assert agg["h_end_utc"] == "2025-01-03T10:05:00"
    assert agg["complete_bucket_count"] == 1
    assert agg["incomplete_bucket_count"] == 0
    assert report["suitable_for_5m_aggregation"] is True


def test_incomplete_tail_row_double_protection(tmp_path):
    """Обязательный пример 8.3 №4: последняя строка 10:04 с is_complete=false,
    последняя завершённая — 10:03. Бакет incomplete дважды: и по (a), и по (b)."""
    rows = make_csv_rows(5, start_hh=10, complete_map={4: "false"})
    csv_path = write_csv(tmp_path / "tail_false.csv", rows)
    report = suit.build_report(csv_path, 2025)
    agg = report["aggregation_5m"]
    # H_end только по завершённым: 10:03 + 1 мин = 10:04 (false-строка не продлевает)
    assert agg["last_complete_1m_start_utc"] == "2025-01-03T10:03:00"
    assert agg["h_end_utc"] == "2025-01-03T10:04:00"
    assert agg["complete_bucket_count"] == 0
    e = agg["excluded_incomplete_buckets"][0]
    assert e["reason"] == "contains_incomplete_row"
    assert e["rows_is_complete_false"] == 1
