#!/usr/bin/env python3
# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А, скрипт 4/4
# report_suitability_5m.py — отчёт о пригодности нормализованных
# архивных данных 1M для последующей агрегации 1M -> 5M
# (по спецификации раздела 8 и проверкам консервации 9.3 плана v2.2).
#
# RESEARCH ONLY. READ ONLY. ADDITIVE ONLY. NO OVERWRITE.
#
# Исправления раунда ревью 1 (P2):
# 1. Проверка time_sorted выполняется по ИСХОДНОЙ последовательности
#    строк ДО какой-либо сортировки (раньше проверка выполнялась после
#    сортировки и была тривиально истинной).
# 2. Фактические значения is_complete входного CSV проверяются и
#    публикуются (true/false/invalid счётчики).
# 3. Строки с is_complete=false не попадают в «завершённые» данные
#    без явного учёта: они участвуют в консервации уровня A наравне
#    со всеми (план 9.3), но их объём отдельно посчитан, а их бакеты
#    исключены из complete с явной причиной.
# 4. Правило завершённости 5M-бакета = план 8.3.2 (ОБА условия):
#    a) is_complete ВСЕХ входящих строк == true;
#    b) T + 5 мин <= H_end, где горизонт считается ТОЛЬКО по строкам
#       с is_complete=true (H_end = начало последней завершённой
#       минуты + 1 мин, план 8.3.1); uncompleted-свеча горизонт
#       не продлевает.
# 5. Причины исключения — из перечня плана 9.6:
#    contains_incomplete_row | tail_window_beyond_horizon.
#
# Согласованные правила агрегации плана v2.2 НЕ меняются:
# сетка UTC floor(t, 5min); open=первая, close=последняя, high=max,
# low=min, volume=sum; консервация A1/A2 до фильтрации, B1/B2 после.
# ============================================================

import argparse
import csv
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import hal_common as common
import hal_config as cfg

BUCKET_MINUTES = 5


def read_normalized_csv(path: Path) -> dict:
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"time", "open", "high", "low", "close", "volume", "is_complete"}
        fields = set(reader.fieldnames or [])
        if not required.issubset(fields):
            return {"error": f"отсутствуют обязательные колонки: {sorted(required - fields)}"}
        for row in reader:
            rows.append(row)
    return {"rows": rows, "fields": sorted(fields)}


def parse_rows(rows: list) -> dict:
    """Разбор строк + ПРОВЕРКА ФАКТИЧЕСКИХ значений is_complete (P2.2).

    is_complete парсится строго: 'true'/'false' (регистр и пробелы
    нормализуются); любое иное значение — is_complete_invalid и для
    правила завершённости трактуется консервативно как False.
    """
    parsed = []
    problems = {"time_unparseable": 0, "price_unparseable": 0,
                "volume_invalid": 0, "is_complete_invalid": 0}
    ic_values = {"true": 0, "false": 0, "invalid": 0}
    for row in rows:
        dt, _ = common.parse_time_any(row["time"])
        if dt is None:
            problems["time_unparseable"] += 1
            continue
        try:
            o = Decimal(row["open"])
            h = Decimal(row["high"])
            l = Decimal(row["low"])
            c = Decimal(row["close"])
        except Exception:  # noqa: BLE001
            problems["price_unparseable"] += 1
            continue
        vol = common.parse_volume_int(row["volume"])
        if vol is None or vol < 0:
            problems["volume_invalid"] += 1
            continue
        raw_ic = str(row.get("is_complete", "")).strip().lower()
        if raw_ic == "true":
            ic = True
        elif raw_ic == "false":
            ic = False
        else:
            ic = False                      # консервативно: incomplete
            problems["is_complete_invalid"] += 1
        ic_values["true" if ic and raw_ic == "true" else
                  ("false" if raw_ic == "false" else "invalid")] += 1
        parsed.append((dt, o, h, l, c, vol, ic))
    return {"parsed": parsed, "problems": problems, "is_complete_values": ic_values}


def aggregate_5m(parsed: list) -> dict:
    """Полная агрегация (все бакеты, включая incomplete) — план 8.1-8.3 v2.2.

    Завершённость бакета — план 8.3.2 (ОБА условия):
    a) is_complete всех исходных строк бакета == true;
    b) T + 5 мин <= H_end, где H_end строится ТОЛЬКО по строкам с
       is_complete=true (план 8.3.1); агрегатор никогда не «повышает»
       завершённость (план 8.3.7).
    Строки с is_complete=false — валидные строки: участвуют в
    консервации уровня A наравне со всеми (план 9.3), их объём
    учтён отдельно и целиком лежит в исключённых бакетах.
    """
    if not parsed:
        return {"error": "нет валидных строк"}
    parsed = sorted(parsed, key=lambda x: x[0])

    # Горизонт H_end — ТОЛЬКО по строкам is_complete=true (план 8.3.1):
    # H_end = (начало последней завершённой минуты) + 1 минута.
    complete_starts = [t for t, _o, _h, _l, _c, _v, ic in parsed if ic]
    h_end = max(complete_starts) + timedelta(minutes=1) if complete_starts else None

    buckets = defaultdict(list)
    for dt, o, h, l, c, v, ic in parsed:
        minute_index = (dt.hour * 60 + dt.minute) // BUCKET_MINUTES
        bucket_start = dt.replace(hour=minute_index * BUCKET_MINUTES // 60,
                                  minute=minute_index * BUCKET_MINUTES % 60,
                                  second=0, microsecond=0)
        buckets[bucket_start].append((dt, o, h, l, c, v, ic))

    all_buckets = []
    volume_all = Decimal(0)
    minutes_all = 0
    volume_complete = Decimal(0)
    minutes_complete = 0
    volume_excluded = Decimal(0)
    minutes_excluded = 0
    complete_count = 0
    incomplete_count = 0
    rows_is_complete_false = sum(1 for *_x, ic in parsed if not ic)
    volume_of_incomplete_rows = sum(
        (v for _t, _o, _h, _l, _c, v, ic in parsed if not ic), Decimal(0))
    volume_of_incomplete_rows_in_excluded = Decimal(0)
    incomplete_rows_in_complete_buckets = 0
    excluded_details = []

    for bucket_start in sorted(buckets):
        items = sorted(buckets[bucket_start], key=lambda x: x[0])
        volume_sum = sum((v for _t, _o, _h, _l, _c, v, _ic in items), Decimal(0))
        minutes_seen = len(items)
        bucket_incomplete_rows = sum(1 for *_x, ic in items if not ic)
        # План 8.3.2: завершённость = (a) все строки true И (b) T+5 <= H_end
        all_rows_complete = bucket_incomplete_rows == 0
        time_ok = (h_end is not None
                   and bucket_start + timedelta(minutes=BUCKET_MINUTES) <= h_end)
        is_complete = all_rows_complete and time_ok
        bucket = {
            "bucket_start_utc": bucket_start.isoformat(),
            "open": str(items[0][1].normalize()),
            "high": str(max(i[2] for i in items).normalize()),
            "low": str(min(i[3] for i in items).normalize()),
            "close": str(items[-1][4].normalize()),
            "volume": int(volume_sum),
            "minutes_seen": minutes_seen,
            "rows_is_complete_false": bucket_incomplete_rows,
            "is_complete": bool(is_complete),
        }
        all_buckets.append(bucket)
        volume_all += volume_sum
        minutes_all += minutes_seen
        if is_complete:
            complete_count += 1
            volume_complete += volume_sum
            minutes_complete += minutes_seen
            incomplete_rows_in_complete_buckets += bucket_incomplete_rows
        else:
            incomplete_count += 1
            volume_excluded += volume_sum
            minutes_excluded += minutes_seen
            volume_of_incomplete_rows_in_excluded += sum(
                (v for _t, _o, _h, _l, _c, v, ic in items if not ic), Decimal(0))
            reason = ("contains_incomplete_row" if not all_rows_complete
                      else "tail_window_beyond_horizon")
            excluded_details.append({
                "bucket_start_utc": bucket_start.isoformat(),
                "minutes_seen": minutes_seen,
                "volume": int(volume_sum),
                "rows_is_complete_false": bucket_incomplete_rows,
                "reason": reason,
            })

    volume_input = sum((_v for _t, _o, _h, _l, _c, _v, _ic in parsed), Decimal(0))
    a1_ok = volume_input == volume_all
    a2_ok = minutes_all == len(parsed)
    b1_ok = volume_all == volume_complete + volume_excluded
    b2_ok = minutes_all == minutes_complete + minutes_excluded
    return {
        "h_end_utc": h_end.isoformat() if h_end else None,
        "h_end_basis": ("последняя минута с is_complete=true + 1 мин (план 8.3.1)"
                        if complete_starts else "не определён: нет строк is_complete=true"),
        "last_complete_1m_start_utc": (max(complete_starts).isoformat()
                                       if complete_starts else None),
        "bucket_count": len(all_buckets),
        "complete_bucket_count": complete_count,
        "incomplete_bucket_count": incomplete_count,
        "volume_1m_input": int(volume_input),
        "volume_5m_all_buckets": int(volume_all),
        "volume_complete_buckets": int(volume_complete),
        "volume_excluded_incomplete_buckets": int(volume_excluded),
        "minutes_seen_all_buckets": minutes_all,
        "valid_1m_input_rows": len(parsed),
        "minutes_in_complete_buckets": minutes_complete,
        "minutes_in_excluded_incomplete_buckets": minutes_excluded,
        "rows_is_complete_false": rows_is_complete_false,
        "volume_of_incomplete_rows": int(volume_of_incomplete_rows),
        "volume_of_incomplete_rows_in_excluded_buckets":
            int(volume_of_incomplete_rows_in_excluded),
        "incomplete_rows_fully_inside_excluded_buckets": bool(
            volume_of_incomplete_rows == volume_of_incomplete_rows_in_excluded
            and incomplete_rows_in_complete_buckets == 0),
        "conservation": {
            "A1_sum_volume_1m_eq_all_buckets": bool(a1_ok),
            "A2_minutes_eq_rows": bool(a2_ok),
            "B1_volume_split_balance": bool(b1_ok),
            "B2_minutes_split_balance": bool(b2_ok),
        },
        "excluded_incomplete_buckets": excluded_details[:50],
        "excluded_total": len(excluded_details),
    }


def day_density(parsed: list) -> dict:
    by_day = defaultdict(list)
    for dt, _o, _h, _l, _c, _v, _ic in parsed:
        by_day[dt.date()].append(dt)
    days = []
    for day, times in sorted(by_day.items()):
        times = sorted(set(times))
        span = (times[-1] - times[0]).total_seconds() // 60 + 1
        days.append({
            "date": day.isoformat(),
            "first_utc": times[0].strftime("%H:%M:%S"),
            "last_utc": times[-1].strftime("%H:%M:%S"),
            "first_msk": common.msk(times[0]).strftime("%H:%M:%S"),
            "last_msk": common.msk(times[-1]).strftime("%H:%M:%S"),
            "minutes_seen": len(times),
            "span_minutes": int(span),
            "missing_within_span": int(span - len(times)),
        })
    return {"day_count": len(days), "days": days}


def build_report(normalized_csv: Path, requested_year: int) -> dict:
    report = {
        "script": "report_suitability_5m.py",
        "stage": "2A",
        "created_utc": common.iso_utc(common.utc_now()),
        "normalized_csv": str(normalized_csv),
        "requested_year": requested_year,
        "synthetic": False,
        "bucket_minutes": BUCKET_MINUTES,
    }
    data = read_normalized_csv(normalized_csv)
    if "error" in data:
        report["error"] = data["error"]
        return report
    parsed_result = parse_rows(data["rows"])
    report["input_rows"] = len(data["rows"])
    report["parse_problems"] = parsed_result["problems"]
    report["is_complete_values"] = parsed_result["is_complete_values"]
    parsed = parsed_result["parsed"]
    if not parsed:
        report["error"] = "нет валидных строк после разбора"
        return report

    # P2.1: проверка сортировки — по ИСХОДНОЙ последовательности строк,
    # ДО любой сортировки (aggregate_5m сортирует собственную копию).
    original_keys = [t for t, *_rest in parsed]
    report["time_sorted"] = original_keys == sorted(original_keys)
    report["time_unique"] = len(set(original_keys)) == len(original_keys)
    report["minute_aligned"] = all(t.second == 0 and t.microsecond == 0
                                   for t in original_keys)
    parsed.sort(key=lambda x: x[0])
    keys = [t for t, *_rest in parsed]
    years = {t.year for t in keys}
    report["years_present"] = sorted(years)
    report["within_requested_year"] = years == {requested_year}

    agg = aggregate_5m(parsed)
    report["aggregation_5m"] = agg
    report["day_density"] = day_density(parsed)

    blockers = []
    if not report["time_sorted"]:
        blockers.append("входные строки не отсортированы по time "
                        "(проверено по исходному порядку до сортировки)")
    if not report["time_unique"]:
        blockers.append("дубликаты ключа time (план 8.4 — недопустимо)")
    if not report["minute_aligned"]:
        blockers.append("метки времени не выровнены на начало минуты")
    if not report["within_requested_year"]:
        blockers.append(f"метки вне запрошенного года: {report['years_present']}")
    if any(parsed_result["problems"].values()):
        blockers.append(f"неразобранные строки: {parsed_result['problems']}")
    ic_vals = parsed_result["is_complete_values"]
    if ic_vals["invalid"]:
        blockers.append(f"некорректные значения is_complete: {ic_vals['invalid']} "
                        "(допустимо 'true'/'false')")
    if ic_vals["false"]:
        blockers.append(
            f"строки с is_complete=false: {ic_vals['false']} "
            "(архивные строки должны быть true по плану 8.3.5; в агрегации "
            "учтены явно: их бакеты исключены из complete с причиной "
            "contains_incomplete_row)")
    if agg.get("h_end_utc") is None and "error" not in agg:
        blockers.append("нет строк с is_complete=true — H_end не определён "
                        "(план 8.3.1); все бакеты исключены")
    if agg.get("conservation"):
        cons = agg["conservation"]
        for name in ("A1_sum_volume_1m_eq_all_buckets", "A2_minutes_eq_rows",
                     "B1_volume_split_balance", "B2_minutes_split_balance"):
            if not cons.get(name):
                blockers.append(f"консервация не сошлась: {name} (план 9.3)")
        if not agg.get("incomplete_rows_fully_inside_excluded_buckets", True):
            blockers.append("строки is_complete=false попали в complete-бакеты "
                            "(нарушение плана 8.3.2a) — явный учёт нарушен")
    elif "error" not in agg:
        blockers.append("агрегация не выполнена")

    report["blockers"] = blockers
    report["suitable_for_5m_aggregation"] = not blockers
    report["verdict_note"] = (
        "вердикт ограничен этапом 2А: структура и статика одного архива. "
        "time_sorted проверен по исходному порядку строк до сортировки; "
        "завершённость бакетов — план 8.3.2 (is_complete всех строк И "
        "T+5 <= H_end; горизонт только по строкам is_complete=true). "
        "НЕ подтверждено на 2А: единицы объёма эмпирически (класс 3, план 10.2), "
        "полнота по торговому календарю (класс 4-5, план 10.2; coverage 9.6), "
        "суточное обновление (2Б), 429/лимиты (2В), статус sealed года "
        "(8.3.2b-альтернатива — вне 2А)"
    )
    return report


def write_markdown(report: dict, path: Path) -> None:
    lines = ["# Отчёт пригодности 1M -> 5M (этап 2А)", ""]
    if report.get("error"):
        lines.append(f"ОШИБКА: {report['error']}")
    else:
        agg = report.get("aggregation_5m", {})
        cons = agg.get("conservation", {})
        ic_vals = report.get("is_complete_values", {})
        lines += [
            f"- источник: `{report.get('normalized_csv')}`",
            f"- входных строк: {report.get('input_rows')}; "
            f"проблем разбора: {report.get('parse_problems')}",
            f"- time уникален: **{report.get('time_unique')}**; отсортирован "
            f"(исходный порядок ДО сортировки): **{report.get('time_sorted')}**; "
            f"выровнен на минуту: **{report.get('minute_aligned')}**",
            f"- is_complete в данных: true={ic_vals.get('true', 0)}, "
            f"false={ic_vals.get('false', 0)}, invalid={ic_vals.get('invalid', 0)}",
            f"- годы в данных: {report.get('years_present')} (запрошен "
            f"{report.get('requested_year')})",
            "",
            "## Агрегация (имитация, все бакеты)",
            "",
            f"- бакетов 5M: {agg.get('bucket_count')} (complete: "
            f"{agg.get('complete_bucket_count')}, incomplete: "
            f"{agg.get('incomplete_bucket_count')})",
            f"- H_end: {agg.get('h_end_utc')} ({agg.get('h_end_basis')}); "
            f"последняя завершённая минута: {agg.get('last_complete_1m_start_utc')}",
            f"- строк is_complete=false: {agg.get('rows_is_complete_false')} "
            f"(объём {agg.get('volume_of_incomplete_rows')}); все они лежат в "
            f"исключённых бакетах: "
            f"**{agg.get('incomplete_rows_fully_inside_excluded_buckets')}**",
            "",
            "## Консервация (план 9.3, уровни A/B)",
            "",
            f"- A1: {cons.get('A1_sum_volume_1m_eq_all_buckets')} "
            f"(1M={agg.get('volume_1m_input')} == 5M all={agg.get('volume_5m_all_buckets')})",
            f"- A2: {cons.get('A2_minutes_eq_rows')} "
            f"({agg.get('minutes_seen_all_buckets')} == "
            f"{agg.get('valid_1m_input_rows')})",
            f"- B1: {cons.get('B1_volume_split_balance')} "
            f"(complete={agg.get('volume_complete_buckets')} + "
            f"excluded={agg.get('volume_excluded_incomplete_buckets')})",
            f"- B2: {cons.get('B2_minutes_split_balance')} "
            f"({agg.get('minutes_in_complete_buckets')} + "
            f"{agg.get('minutes_in_excluded_incomplete_buckets')})",
            f"- исключённых incomplete-бакетов: {agg.get('excluded_total')} "
            f"(первые: {agg.get('excluded_incomplete_buckets')[:5]})",
            "",
            "## Дни",
            "",
            f"- дней: {report.get('day_density', {}).get('day_count')}",
            "",
            "```",
        ]
        for d in report.get("day_density", {}).get("days", [])[:10]:
            lines.append(
                f"{d['date']}  {d['first_msk']}-{d['last_msk']} МСК  "
                f"минут={d['minutes_seen']}  span={d['span_minutes']}  "
                f"пропуски в span={d['missing_within_span']}"
            )
        lines += ["```", ""]
        lines += [
            f"## Вердикт: suitable_for_5m_aggregation = "
            f"**{report.get('suitable_for_5m_aggregation')}**",
            "",
        ]
        if report.get("blockers"):
            for b in report["blockers"]:
                lines.append(f"- блокер: {b}")
        lines.append("")
        lines.append(report.get("verdict_note", ""))
    common.atomic_write_text(path, "\n".join(lines) + "\n")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Этап 2А: отчёт пригодности для агрегации 1M -> 5M"
    )
    parser.add_argument("--normalized", required=True,
                        help="путь к нормализованному CSV (скрипт 3)")
    parser.add_argument("--year", type=int, default=cfg.PROTOTYPE_YEAR)
    parser.add_argument("--label", default=None, help="метка артефакта")
    parser.add_argument("--synthetic", action="store_true",
                        help="пометить отчёт как анализ синтетического фиксёра")
    return parser


def main() -> int:
    import sys

    args = build_arg_parser().parse_args()
    normalized = Path(args.normalized)
    if not normalized.exists():
        common.log(f"Файл не найден: {normalized}")
        return 2
    report = build_report(normalized, args.year)
    report["synthetic"] = bool(args.synthetic)
    if args.label:
        report["label"] = args.label
    cfg.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    label = args.label or normalized.stem
    stamp = common.utc_now().strftime("%Y%m%dT%H%M%SZ")
    json_path = cfg.REPORTS_DIR / f"suitability_5m_{label}_{stamp}.json"
    md_path = cfg.REPORTS_DIR / f"suitability_5m_{label}_{stamp}.md"
    common.atomic_write_json(json_path, report)
    write_markdown(report, md_path)
    common.log(f"Отчёт: {json_path}")
    common.log(f"Сводка: {md_path}")
    common.log(f"suitable_for_5m_aggregation = {report.get('suitable_for_5m_aggregation')}")
    return 0 if report.get("suitable_for_5m_aggregation") else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
