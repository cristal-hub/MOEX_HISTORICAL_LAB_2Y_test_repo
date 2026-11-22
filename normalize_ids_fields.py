#!/usr/bin/env python3
# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А, скрипт 3/4
# normalize_ids_fields.py — проверка идентификаторов инструмента
# и нормализация полей архива в схему лаборатории.
#
# RESEARCH ONLY. READ ONLY. ADDITIVE ONLY. NO OVERWRITE.
#
# Что делает:
# 1. Определяет фактическую структуру CSV (те же детекторы, что и
#    inspect_zip.py — допущения не вводятся).
# 2. Проверяет идентификатор в строках против запрошенного
#    инструмента по полю, фактически встреченному в архиве
#    (план 9.1.3); расхождение -> STOP по инструменту.
# 3. Нормализует строки в схему лаборатории
#    time,open,high,low,close,volume,is_complete:
#    - цены как Decimal из исходных строк (план 10.3), без float;
#    - UTC-парсинг времени (план 9.2); is_complete=true для архивных
#      строк с обоснованием план 8.3.5 (архив содержит только
#      прошедшие дни; интервалы закрыты по построению);
#    - сортировка по time, assert-скан дубликатов (конфликт значений
#      -> FAILED-счётчик, тихое склеивание запрещено, план 8.4/9.2);
#    - валидация строки (план 9.2): isfinite-цены, OHLC-правила,
#      volume целый >= 0;
#    - доля отбракованных строк > 0.5% -> флаг FAILED чанка (9.2.5).
# 4. Пишет нормализованный CSV (utf-8 БЕЗ BOM — совместимость с
#    research-контрактом, план 3.7.8) + отчёт нормализации.
#
# Существующие данные лаборатории не читаются и не изменяются.
# ============================================================

import argparse
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import hal_common as common
import hal_config as cfg
import inspect_zip as inspector

CANONICAL_FIELDS = ["time", "open", "high", "low", "close", "volume", "is_complete"]


def decode_and_rows(raw: bytes) -> dict:
    decoded = inspector.decode_bytes(raw)
    if not decoded["decodable"]:
        return {"error": "не декодируется (utf-8/cp1251)"}
    delimiter = inspector.detect_delimiter(decoded["sample_text"])
    body = raw[3:] if decoded["has_bom"] else raw
    text = body.decode(decoded["encoding"])
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header_present = False
    header_names = []
    if rows:
        first_tags = [inspector.classify_value(v) for v in rows[0]]
        looks_data = [any(("decimal" in t or "int" in t or "time" in t
                           or "uuid" in t or "figi" in t) for t in ft)
                      for ft in first_tags]
        header_present = not all(looks_data)
    if header_present:
        header_names = rows[0]
        rows = rows[1:]
    return {"encoding": decoded["encoding"], "has_bom": decoded["has_bom"],
            "delimiter": delimiter, "header_present": header_present,
            "header_names": header_names, "rows": rows}


def verify_identifier(rows: list, roles: dict) -> dict:
    """Проверка ID в строках против запрошенного инструмента (план 9.1.3)."""
    if "id" not in roles:
        return {"verified": False, "reason": "ID-колонка не определена",
                "mismatch_rows": None, "stop": True}
    id_index = roles["id"]
    id_kind = roles.get("id_kind")
    expected_uid = cfg.INSTRUMENT["uid"]
    expected_figi = cfg.INSTRUMENT["figi"]
    mismatches = 0
    first_value = None
    for row in rows:
        value = str(row[id_index]).strip() if id_index < len(row) else ""
        if first_value is None:
            first_value = value
        if value != expected_uid and value != expected_figi:
            mismatches += 1
    verified = mismatches == 0
    return {
        "verified": verified,
        "id_kind_detected": id_kind,
        "matches_uid": first_value == expected_uid,
        "matches_figi": first_value == expected_figi,
        "mismatch_rows": mismatches,
        "stop": not verified,
        "note": "сверка против обоих идентификаторов конфига; при запросе "
                "instrument_id=uid совпадение по figi фиксируется как "
                "«ID того же инструмента по локальному гайд-дампу» и "
                "требует перекрёстной сверки с реестром",
    }


def normalize_rows(rows: list, roles: dict, requested_year: int) -> dict:
    """Нормализация + валидация (план 9.2). Возвращает факты и валидные строки."""
    ti = roles.get("utc")
    price_cols = roles.get("price_columns", [])
    vi = roles.get("volume")
    ohlc = roles.get("ohlc_mapping") or {}
    chosen = ohlc.get("chosen")
    mapping = ohlc.get(chosen, {}).get("mapping") if chosen else None
    result = {"valid_rows": [], "rejected": [], "raw_prices_map": {},
              "total_rows": len(rows), "duplicates_dropped": 0,
              "conflicting_duplicates": 0, "out_of_year": 0,
              "rejected_reasons": Counter(), "seen_keys": set()}
    if not mapping or ti is None or vi is None:
        result["fatal"] = "структура CSV не позволяет нормализацию (роли не определены)"
        return result

    parse_buffer = []
    for row in rows:
        raw_time = str(row[ti]).strip() if ti < len(row) else ""
        dt, _fmt = common.parse_time_any(raw_time)
        if dt is None:
            result["rejected"].append({"time_raw": raw_time, "reason": "time_unparseable"})
            result["rejected_reasons"]["time_unparseable"] += 1
            continue
        prices = {}
        raw_prices = {}
        ok = True
        for name in ("open", "close", "high", "low"):
            idx = mapping.get(name)
            raw_cell = row[idx] if idx is not None and idx < len(row) else ""
            okd, d, reason = common.parse_decimal_strict(raw_cell)
            if not okd:
                result["rejected"].append({"time_raw": raw_time,
                                           "reason": f"{name}_{reason}"})
                result["rejected_reasons"][f"{name}_{reason}"] += 1
                ok = False
                break
            prices[name] = d
            raw_prices[name] = str(raw_cell).strip()
        if not ok:
            continue
        vol = common.parse_volume_int(row[vi] if vi < len(row) else "")
        if vol is None or vol < 0:
            result["rejected"].append({"time_raw": raw_time, "reason": "volume_invalid"})
            result["rejected_reasons"]["volume_invalid"] += 1
            continue
        valid, _reasons = common.validate_candle_row(
            prices["open"], prices["close"], prices["high"], prices["low"], vol)
        if not valid:
            result["rejected"].append({"time_raw": raw_time, "reason": "ohlc_validation"})
            result["rejected_reasons"]["ohlc_validation"] += 1
            continue
        if dt.year != requested_year:
            result["out_of_year"] += 1
            result["rejected"].append({"time_raw": raw_time, "reason": "out_of_year"})
            result["rejected_reasons"]["out_of_year"] += 1
            continue
        parse_buffer.append((dt, prices, vol, raw_prices))

    parse_buffer.sort(key=lambda x: x[0])

    for dt, prices, vol, raw_prices in parse_buffer:
        key = dt.isoformat()
        if key in result["seen_keys"]:
            # Дедуп-скан: конфликт значений -> FAILED-счётчик (план 8.4);
            # совпадение значений -> дубликат отброшен с учётом
            result["duplicates_dropped"] += 1
            for prev in result["valid_rows"]:
                if prev[0] == key:
                    if (prev[1] != prices or prev[2] != vol):
                        result["conflicting_duplicates"] += 1
                    break
            continue
        result["seen_keys"].add(key)
        result["valid_rows"].append((key, prices, vol))
        result["raw_prices_map"][key] = raw_prices

    result["valid_count"] = len(result["valid_rows"])
    result["rejected_count"] = len(result["rejected"])
    result["rejected_share"] = (
        result["rejected_count"] / result["total_rows"] if result["total_rows"] else 0.0
    )
    result["failed_by_rejected_share"] = (
        result["rejected_share"] > cfg.MAX_REJECTED_ROW_SHARE
    )
    return result


def format_price(d: Decimal) -> str:
    """Fallsback: каноническое десятичное представление Decimal (план 10.3).

    Основной режим — запись исходных строк архива без изменений
    (write_normalized_csv с raw_prices_map), что сохраняет точную
    запись источника для аудита.
    """
    return format(d.normalize(), "f") if d != 0 else "0"


def write_normalized_csv(path: Path, valid_rows: list,
                         raw_prices_map: dict | None = None) -> None:
    import io as _io

    raw_prices_map = raw_prices_map or {}
    buffer = _io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CANONICAL_FIELDS)
    for key, prices, vol in valid_rows:
        raws = raw_prices_map.get(key, {})
        writer.writerow([
            key,
            raws.get("open") or format_price(prices["open"]),
            raws.get("high") or format_price(prices["high"]),
            raws.get("low") or format_price(prices["low"]),
            raws.get("close") or format_price(prices["close"]),
            vol,
            "true",  # план 8.3.5: архив содержит только прошедшие дни
        ])
    common.atomic_write_text(path, buffer.getvalue())


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Этап 2А: проверка идентификаторов и нормализация полей ZIP"
    )
    parser.add_argument("--zip", required=True, help="путь к ZIP-архиву")
    parser.add_argument("--year", type=int, default=cfg.PROTOTYPE_YEAR)
    parser.add_argument("--label", default=None, help="метка артефакта")
    parser.add_argument("--synthetic", action="store_true",
                        help="пометить артефакты как синтетические (фиксёр)")
    parser.add_argument("--sample", type=int, default=20000,
                        help="строк для детекции структуры (нормализация — всегда полная)")
    parser.add_argument("--max-output-rows", type=int, default=0,
                        help="ограничить число строк нормализованного CSV "
                             "(0 = без ограничения; прототип-демо)")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    zip_path = Path(args.zip)
    if not zip_path.exists():
        common.log(f"ZIP не найден: {zip_path}")
        return 2

    report = {
        "script": "normalize_ids_fields.py",
        "stage": "2A",
        "created_utc": common.iso_utc(common.utc_now()),
        "requested_year": args.year,
        "source_zip": str(zip_path),
        "synthetic": bool(args.synthetic),
        "label": args.label,
    }

    target, all_members = inspector.largest_csv_member(zip_path)
    if target is None:
        report["fatal"] = "CSV-член внутри ZIP не найден"
        common.log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    report["target_member"] = target.filename
    report["all_members"] = all_members

    with ZipFile(zip_path) as zf:
        with zf.open(target) as raw_file:
            raw = raw_file.read()
    structure = decode_and_rows(raw)
    if "error" in structure:
        report["fatal"] = structure["error"]
        common.log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    rows = structure["rows"]
    report["structure"] = {
        "encoding": structure["encoding"], "has_bom": structure["has_bom"],
        "delimiter": repr(structure["delimiter"]),
        "header_present": structure["header_present"],
        "header_names": structure["header_names"],
        "row_count": len(rows),
    }

    analysis_rows = rows[: args.sample] if args.sample else rows
    columns = inspector.analyze_columns(analysis_rows)
    roles = inspector.infer_roles(columns, analysis_rows)
    report["roles"] = roles

    id_check = verify_identifier(rows, roles)
    report["identifier_check"] = id_check

    norm = normalize_rows(rows, roles, args.year)
    if "fatal" in norm:
        report["fatal"] = norm["fatal"]
        common.log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    report["normalization"] = {
        k: (dict(v) if k == "rejected_reasons" else v)
        for k, v in norm.items() if k not in ("valid_rows", "seen_keys", "rejected")
    }
    report["rejected_examples"] = norm["rejected"][:20]
    if norm.get("out_of_year"):
        common.log(
            f"FAILED: метки времени вне календарного года {args.year}: "
            f"{norm['out_of_year']} (план 9.1.4); нормализованный CSV "
            "не записывается"
        )
        stamp = common.utc_now().strftime("%Y%m%dT%H%M%SZ")
        common.atomic_write_json(
            cfg.NORMALIZED_DIR / f"normalize_report_{args.label or zip_path.stem}_{stamp}.json",
            report,
        )
        return 1

    cfg.NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    label = args.label or zip_path.stem
    ticker = cfg.INSTRUMENT["ticker"]
    prefix = f"{ticker}__{cfg.INSTRUMENT['uid']}_{args.year}"
    out_csv = cfg.NORMALIZED_DIR / f"{prefix}_{label}.normalized.csv"
    valid_rows = norm["valid_rows"]
    if args.max_output_rows and args.max_output_rows < len(valid_rows):
        valid_rows = valid_rows[: args.max_output_rows]
        report["output_truncated_to"] = args.max_output_rows
    write_normalized_csv(out_csv, valid_rows, norm.get("raw_prices_map"))
    report["normalized_csv"] = str(out_csv)
    report["normalized_rows_written"] = len(valid_rows)
    report["canonical_fields"] = CANONICAL_FIELDS
    report["is_complete_semantics"] = (
        "все строки true — обоснование план 8.3.5 (архив только по прошедшим дням)"
    )
    report["volume_unit_note"] = (
        "объём переписан как есть (гипотеза 'лот' по документации 1.2.3, "
        "эмпирическая сверка — класс 3 плана 10, вне этапа 2А)"
    )

    stamp = common.utc_now().strftime("%Y%m%dT%H%M%SZ")
    report_path = cfg.NORMALIZED_DIR / f"normalize_report_{label}_{stamp}.json"
    common.atomic_write_json(report_path, report)

    common.log(f"Нормализованный CSV: {out_csv}")
    common.log(f"Валидных строк: {report['normalization'].get('valid_count')}; "
               f"отбраковано: {report['normalization'].get('rejected_count')} "
               f"(доля {report['normalization'].get('rejected_share'):.6f}); "
               f"дублей отброшено: {report['normalization'].get('duplicates_dropped')}; "
               f"конфликтных дублей: {report['normalization'].get('conflicting_duplicates')}")
    if id_check.get("stop"):
        common.log("STOP: проверка идентификатора не пройдена (план 9.1.3)")
        return 1
    if norm.get("failed_by_rejected_share"):
        common.log("FAILED: доля отбракованных строк > 0.5% года (план 9.2.5)")
        return 1
    if report["normalization"].get("conflicting_duplicates"):
        common.log("FAILED: конфликтующие дубликаты ключа time (план 8.4)")
        return 1
    common.log("OK: нормализация завершена")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
