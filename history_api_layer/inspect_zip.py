#!/usr/bin/env python3
# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А, скрипт 2/4
# inspect_zip.py — инспекция фактической структуры годового ZIP
# архива T-Invest History API и его CSV-содержимого.
#
# RESEARCH ONLY. READ ONLY. Существующие файлы не изменяются.
#
# Принцип: формат данных НЕ предполагается заранее. Инспектор
# определяет по фактическому содержимому: кодировку, разделитель,
# наличие строки заголовка, роли колонок (ID/время/цены/объём),
# фактический порядок полей, формат времени и гипотезу TZ, дубли,
# пропуски, границы сессий, единицы объёма (гипотеза), CRC ZIP.
# Документированное описание (план 1.2) используется только как
# ссылка для СВЕРКИ, не как допущение для парсинга.
# ============================================================

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

import hal_common as common
import hal_config as cfg


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# ---------- ZIP-уровень ----------

def zip_facts(zip_path: Path) -> dict:
    facts = {"zip_path": str(zip_path), "is_zip": False,
             "crc_ok": False, "members": [], "bad_member": None}
    if not zip_path.exists():
        facts["error"] = "файл не существует"
        return facts
    facts["file_size"] = zip_path.stat().st_size
    facts["sha256"] = common.sha256_file(zip_path)
    try:
        with ZipFile(zip_path) as zf:
            facts["is_zip"] = True
            facts["members"] = [
                {"name": info.filename, "size": info.file_size,
                 "compressed": info.compress_size, "crc32": info.CRC,
                 "compress_type": info.compress_type}
                for info in zf.infolist()
            ]
            bad = zf.testzip()
            facts["bad_member"] = bad
            facts["crc_ok"] = bad is None
    except Exception as exc:  # noqa: BLE001
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return facts


def largest_csv_member(zip_path: Path):
    with ZipFile(zip_path) as zf:
        csv_members = [i for i in zf.infolist()
                       if i.filename.lower().endswith((".csv", ".txt"))]
        if not csv_members:
            return None, [i.filename for i in zf.infolist()]
        target = max(csv_members, key=lambda i: i.file_size)
        return target, [i.filename for i in zf.infolist()]


# ---------- Декодирование ----------

def decode_bytes(raw: bytes) -> dict:
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if has_bom else raw
    for encoding in ("utf-8", "cp1251"):
        try:
            text = body[:1 << 20].decode(encoding)
            return {"encoding": encoding, "has_bom": has_bom,
                    "decodable": True, "sample_text": text}
        except UnicodeDecodeError:
            continue
    return {"encoding": None, "has_bom": has_bom, "decodable": False,
            "sample_text": ""}


def detect_delimiter(sample_text: str) -> str:
    header_line = sample_text.splitlines()[0] if sample_text else ""
    counts = {",": header_line.count(","), ";": header_line.count(";"),
              "\t": header_line.count("\t"), "|": header_line.count("|")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


# ---------- Классификация значений ----------

def classify_value(value: str) -> set:
    tags = set()
    text = str(value).strip()
    if not text:
        tags.add("empty")
        return tags
    if UUID_RE.match(text):
        tags.add("uuid")
    if common.is_figi_like(text):
        tags.add("figi")
    dt, _label = common.parse_time_any(text)
    if dt is not None:
        tags.add("time")
    vol = common.parse_volume_int(text)
    if vol is not None:
        tags.add("int")
        if vol >= 0:
            tags.add("int_nonneg")
    try:
        d = Decimal(text)
        if d.is_finite():
            tags.add("decimal")
            if d > 0:
                tags.add("decimal_positive")
    except (InvalidOperation, ValueError):
        pass
    if not tags:
        tags.add("other")
    return tags


def analyze_columns(rows: list) -> dict:
    n_cols = max(len(r) for r in rows)
    stats = [defaultdict(int) for _ in range(n_cols)]
    consts = [None] * n_cols
    time_formats = [Counter() for _ in range(n_cols)]

    for row in rows:
        for i in range(n_cols):
            value = row[i] if i < len(row) else ""
            tags = classify_value(value)
            for t in tags:
                stats[i][t] += 1
            if consts[i] is None:
                consts[i] = value
            elif consts[i] != value:
                consts[i] = "__VARIES__"
            dt, label = common.parse_time_any(value)
            if dt is not None and label:
                time_formats[i][label] += 1

    n = len(rows)
    columns = []
    for i in range(n_cols):
        s = stats[i]
        columns.append({
            "index": i,
            "uuid_frac": s["uuid"] / n,
            "figi_frac": s["figi"] / n,
            "time_frac": s["time"] / n,
            "int_frac": s["int"] / n,
            "int_nonneg_frac": s["int_nonneg"] / n,
            "decimal_frac": s["decimal"] / n,
            "decimal_positive_frac": s["decimal_positive"] / n,
            "constant": consts[i] if consts[i] != "__VARIES__" else None,
            "top_time_formats": dict(time_formats[i].most_common(3)),
        })
    return {"n_cols": n_cols, "rows_analyzed": n, "columns": columns}


def infer_roles(columns: dict, rows: list) -> dict:
    """Вывод ролей колонок + эмпирическая проверка порядка ценовых полей."""
    cols = columns["columns"]
    roles = {}

    # ID-колонка: константа, похожая на uid/figi
    for c in cols:
        if c["constant"] and (common.is_uuid_like(c["constant"]) or
                              common.is_figi_like(c["constant"])):
            roles["id"] = c["index"]
            roles["id_kind"] = ("uid" if common.is_uuid_like(c["constant"])
                                else "figi")
            roles["id_value"] = c["constant"]
            break

    # Время: колонка с наибольшей долей распознанного времени
    time_candidates = [c for c in cols if c["time_frac"] >= 0.9]
    if time_candidates:
        best = max(time_candidates, key=lambda c: (c["time_frac"], -c["index"]))
        roles["utc"] = best["index"]

    # Объём: правая колонка с высокой долей неотрицательных целых
    int_candidates = [c for c in cols if c["int_nonneg_frac"] >= 0.9]
    if int_candidates:
        roles["volume"] = max(c["index"] for c in int_candidates)

    # Цены: оставшиеся колонки с высокой долей конечных Decimal
    assigned = {roles.get(k) for k in ("id", "utc", "volume")}
    price_cols = [c["index"] for c in cols
                  if c["decimal_frac"] >= 0.9 and c["index"] not in assigned]
    roles["price_columns"] = price_cols

    if len(price_cols) == 4 and all(k in roles for k in ("utc", "volume")):
        roles["ohlc_mapping"] = _verify_ohlc_order(rows, roles, price_cols)
    return roles


def _verify_ohlc_order(rows: list, roles: dict, price_cols: list) -> dict:
    """Эмпирическая проверка: какой порядок ценовых колонок согласован с OHLC-правилами.

    Кандидаты: документированный (open, close, high, low) и «привычный»
    (open, high, low, close). Вердикт — по доле строк, где
    high >= max(open, close), low <= min(open, close).
    """
    i_open, i_second, i_third, i_fourth = price_cols
    variants = {
        "documented_open_close_high_low": (i_open, i_second, i_third, i_fourth),
        "standard_open_high_low_close": (i_open, i_second, i_third, i_fourth),
    }
    # Оба варианта используют одни и те же индексы; различие — интерпретация
    # 2-й и 4-й колонки. Проверяем фактическую согласованность каждой.
    results = {}
    for name, (io, isec, ithird, ifourth) in variants.items():
        if name == "documented_open_close_high_low":
            mapping = {"open": io, "close": isec, "high": ithird, "low": ifourth}
        else:
            mapping = {"open": io, "high": isec, "low": ithird, "close": ifourth}
        ok = 0
        checked = 0
        for row in rows:
            try:
                o = Decimal(str(row[mapping["open"]]).strip())
                c = Decimal(str(row[mapping["close"]]).strip())
                h = Decimal(str(row[mapping["high"]]).strip())
                l = Decimal(str(row[mapping["low"]]).strip())
            except (InvalidOperation, ValueError, IndexError, TypeError):
                continue
            if not (o.is_finite() and c.is_finite() and h.is_finite() and l.is_finite()):
                continue
            checked += 1
            if h >= max(o, c) and l <= min(o, c) and h >= l:
                ok += 1
        rate = (ok / checked) if checked else 0.0
        results[name] = {"mapping": mapping, "consistent_rate": round(rate, 6),
                         "rows_checked": checked}
    rates = {k: v["consistent_rate"] for k, v in results.items()}
    best = max(rates, key=rates.get)
    worst = min(rates.values())
    # Уверенный выбор: лучший вариант заметно лучше прочих. Абсолютное
    # качество строк проверяется отдельно на нормализации (план 9.2),
    # поэтому порог здесь относительный, не «почти 100%»: единичные
    # невалидные строки не должны ломать определение структуры.
    confident = rates[best] > worst and (rates[best] - worst) >= 0.001
    results["chosen"] = best if confident else None
    results["chosen_rate"] = rates[best]
    results["ambiguous"] = not confident
    results["matches_documented_order"] = (
        best == "documented_open_close_high_low" and confident
    )
    return results


# ---------- Время, сессии, дубли, пропуски ----------

def time_facts(rows: list, roles: dict, requested_year: int) -> dict:
    facts = {"parsed": 0, "unparsed": 0, "formats": Counter(),
             "tz_aware_strings": 0, "naive_strings": 0,
             "second_zero_aligned": 0, "second_nonzero": 0,
             "min_ts": None, "max_ts": None, "out_of_year": 0,
             "duplicate_ts": 0, "duplicate_ts_conflicting": 0,
             "exact_row_duplicates": 0}
    if "utc" not in roles:
        facts["error"] = "время-колонка не определена"
        return facts
    ti = roles["utc"]
    seen_ts = defaultdict(list)
    seen_rows = Counter()
    for row in rows:
        raw = row[ti] if ti < len(row) else ""
        text = str(raw).strip()
        if "Z" in text[-3:] or ("+" in text[-6:] or "-" in text[-6:]):
            facts["tz_aware_strings"] += 1
        else:
            facts["naive_strings"] += 1
        dt, label = common.parse_time_any(text)
        if dt is None:
            facts["unparsed"] += 1
            continue
        facts["parsed"] += 1
        facts["formats"][label] += 1
        if dt.second == 0:
            facts["second_zero_aligned"] += 1
        else:
            facts["second_nonzero"] += 1
        if dt.year != requested_year:
            facts["out_of_year"] += 1
        if facts["min_ts"] is None or dt < facts["min_ts"]:
            facts["min_ts"] = dt
        if facts["max_ts"] is None or dt > facts["max_ts"]:
            facts["max_ts"] = dt
        seen_ts[dt].append(text)
        seen_rows[tuple(row)] += 1

    facts["duplicate_ts"] = sum(1 for v in seen_ts.values() if len(v) > 1)
    facts["duplicate_ts_total_rows"] = sum(len(v) for v in seen_ts.values())
    facts["exact_row_duplicates"] = sum(c - 1 for c in seen_rows.values() if c > 1)
    # Точный скан конфликтов выполняет duplicate_conflict_scan() отдельно
    facts["min_ts"] = (facts["min_ts"].isoformat()
                       if facts.get("min_ts") is not None else None)
    facts["max_ts"] = (facts["max_ts"].isoformat()
                       if facts.get("max_ts") is not None else None)
    return facts


def duplicate_conflict_scan(rows: list, roles: dict) -> dict:
    """Точный скан дубликатов по ts с проверкой различий остальных полей."""
    ti = roles.get("utc")
    idc = roles.get("id")
    if ti is None:
        return {"error": "no time column"}
    groups = defaultdict(list)
    for row in rows:
        dt, _ = common.parse_time_any(str(row[ti]).strip())
        if dt is None:
            continue
        key = dt
        if idc is not None and idc < len(row):
            groups[key].append(row[:ti] + row[ti + 1:] if idc == ti else row)
        else:
            groups[key].append(row)
    dup_ts = sum(1 for v in groups.values() if len(v) > 1)
    conflicting = sum(
        1 for v in groups.values() if len(v) > 1 and len({tuple(r) for r in v}) > 1
    )
    return {"duplicate_ts_groups": dup_ts, "conflicting_groups": conflicting}


def day_session_stats(rows: list, roles: dict, naive_tz_hypothesis: str = "UTC") -> dict:
    """По-дневные first/last минуты и вердикт правдоподобия TZ.

    Наивная метка интерпретируется двумя способами (как UTC и как МСК);
    фиксируется, какая интерпретация лучше согласуется с окнами сессий
    MOEX (СПРАВОЧНЫЕ окна для правдоподобия, не календарь торгов).
    """
    ti = roles.get("utc")
    by_day = defaultdict(list)
    for row in rows:
        dt, _ = common.parse_time_any(str(row[ti]).strip())
        if dt is None:
            continue
        by_day[dt.date()].append(dt)
    days = []
    for day, times in sorted(by_day.items()):
        days.append({
            "date": day.isoformat(),
            "first_utc_hyp": min(times).strftime("%H:%M:%S"),
            "last_utc_hyp": max(times).strftime("%H:%M:%S"),
            "minutes_seen": len(set(times)),
        })
    plaus = {"as_utc": 0, "as_msk": 0}
    earliest_main = cfg.SESSION_PLAUSIBILITY_MSK["main_open_earliest"]
    latest_evening = cfg.SESSION_PLAUSIBILITY_MSK["evening_close_latest"]
    for day in days:
        # Сравнение в гранулярности HH:MM (справочные окна — HH:MM)
        first_h = day["first_utc_hyp"][:5]
        last_h = day["last_utc_hyp"][:5]
        # Гипотеза «наивное = UTC»: в МСК first += 3h
        first_msk = _shift(first_h, cfg.MSK_UTC_OFFSET_HOURS)
        last_msk = _shift(last_h, cfg.MSK_UTC_OFFSET_HOURS)
        if first_msk >= earliest_main and last_msk <= latest_evening:
            plaus["as_utc"] += 1
        # Гипотеза «наивное = МСК»: без сдвига
        if first_h >= earliest_main and last_h <= latest_evening:
            plaus["as_msk"] += 1
    return {
        "day_count": len(days),
        "days_first50": days[:50],
        "tz_plausibility_counts": plaus,
        "tz_hypothesis_preferred": (
            "UTC" if plaus["as_utc"] >= plaus["as_msk"] else "MSK-naive"
        ),
        "reference_windows_msk": cfg.SESSION_PLAUSIBILITY_MSK,
        "note": "окна — справочник правдоподобия, не календарь торгов; "
                "особые/короткие сессии дают менее строгие границы",
    }


def _shift(hhmm: str, hours: int) -> str:
    """Сдвиг HH:MM (или HH:MM:SS) на hours часов; возврат HH:MM."""
    parts = [int(x) for x in hhmm.split(":")]
    h, m = parts[0], parts[1]
    base = datetime(2000, 1, 1, h, m) + timedelta(hours=hours)
    return base.strftime("%H:%M")


def gap_stats(rows: list, roles: dict) -> dict:
    ti = roles.get("utc")
    by_day = defaultdict(set)
    for row in rows:
        dt, _ = common.parse_time_any(str(row[ti]).strip())
        if dt is None:
            continue
        by_day[dt.date()].add(dt)
    worst = []
    for day, times in sorted(by_day.items()):
        if not times:
            continue
        span = (max(times) - min(times)).total_seconds() // 60 + 1
        missing = int(span - len(times))
        worst.append((missing, day.isoformat(), len(times)))
    worst.sort(reverse=True)
    return {
        "days": len(by_day),
        "top_missing_within_span": [
            {"missing_minutes": m, "date": d, "minutes_seen": s}
            for m, d, s in worst[:10]
        ],
        "total_missing_within_spans": sum(m for m, _d, _s in worst),
        "note": "пропуск внутри span != дефект: минута без сделок может "
                "отсутствовать; различение — сверка источников (классы 4-5, план 10.2)",
    }


# ---------- Числовые факты ----------

def numeric_facts(rows: list, roles: dict) -> dict:
    facts = {}
    pi = roles.get("volume")
    price_cols = roles.get("price_columns", [])
    if pi is not None:
        vols = []
        for row in rows:
            v = common.parse_volume_int(row[pi] if pi < len(row) else "")
            if v is not None:
                vols.append(v)
        facts["volume"] = {
            "count": len(vols),
            "min": min(vols) if vols else None,
            "max": max(vols) if vols else None,
            "sum": sum(vols) if vols else 0,
            "zero_count": sum(1 for v in vols if v == 0),
            "all_nonneg": all(v >= 0 for v in vols),
            "documented_unit_hypothesis": cfg.DOCUMENTED_VOLUME_UNIT,
            "unit_verified": False,
            "unit_note": "единицы эмпирически не проверены; сверка с GetCandles "
                         "(класс 3, план 10.2) вне объёма этапа 2А",
        }
    dec_places = Counter()
    bad_prices = 0
    for row in rows:
        for pc in price_cols:
            text = str(row[pc]).strip() if pc < len(row) else ""
            try:
                d = Decimal(text)
            except (InvalidOperation, ValueError, TypeError):
                bad_prices += 1
                continue
            if not d.is_finite():
                bad_prices += 1
                continue
            # Знаков после запятой — из экспоненты Decimal:
            # "260.00" -> 2, "260" -> 0 (равенство Decimal игнорирует нули,
            # поэтому to_integral_value() использовать нельзя)
            exponent = d.as_tuple().exponent
            dec_places[str(max(0, -exponent))] += 1
    facts["prices"] = {
        "columns": price_cols,
        "decimal_places_distribution": dict(dec_places.most_common(5)),
        "unparseable_price_cells": bad_prices,
        "min_price_increment_config": cfg.INSTRUMENT["min_price_increment"],
    }
    return facts


# ---------- Главный вызов ----------

def inspect(zip_path: Path, requested_year: int, sample: int = 20000,
            full: bool = False) -> dict:
    report = {
        "script": "inspect_zip.py",
        "stage": "2A",
        "created_utc": common.iso_utc(common.utc_now()),
        "requested_year": requested_year,
        "synthetic": False,
        "zip_facts": zip_facts(zip_path),
    }
    if report["zip_facts"].get("error") or not zip_path.exists():
        report["error"] = report["zip_facts"].get("error") or "ZIP недоступен"
        return report
    target, all_members = largest_csv_member(zip_path)
    report["all_members"] = all_members
    if target is None:
        report["error"] = "CSV-член внутри ZIP не найден"
        return report
    report["target_member"] = target.filename

    try:
        with ZipFile(zip_path) as zf:
            with zf.open(target) as raw_file:
                raw = raw_file.read()
    except Exception as exc:  # noqa: BLE001 — повреждённый CRC/данные
        report["error"] = f"чтение члена ZIP не удалось: {type(exc).__name__}: {exc}"
        return report
    decoded = decode_bytes(raw)
    report["encoding"] = {k: decoded[k] for k in ("encoding", "has_bom", "decodable")}
    if not decoded["decodable"]:
        report["error"] = "член ZIP не декодируется (utf-8/cp1251)"
        return report
    text = decoded["sample_text"]
    delimiter = detect_delimiter(text)
    report["delimiter"] = repr(delimiter)

    import csv as _csv
    import io

    full_text = raw[3:] if decoded["has_bom"] else raw
    if decoded["encoding"] == "cp1251":
        full_text = raw
    rows = list(_csv.reader(io.StringIO(full_text.decode(decoded["encoding"])),
                            delimiter=delimiter))
    report["row_count"] = len(rows)

    header_present = False
    header_names = []
    if rows:
        first = rows[0]
        first_tags = [classify_value(v) for v in first]
        looks_numeric = [any(("decimal" in t or "int" in t or "time" in t
                              or "uuid" in t or "figi" in t) for t in ft)
                         for ft in first_tags]
        header_present = not all(looks_numeric)
    report["header_present"] = header_present
    if header_present:
        header_names = rows[0]
        rows = rows[1:]
    report["header_names"] = header_names
    report["data_rows"] = len(rows)

    if not rows:
        report["error"] = "данных в CSV нет (пустой архив)"
        return report

    analysis_rows = rows if full else rows[:sample]
    report["analysis_rows"] = len(analysis_rows)
    report["full_mode"] = bool(full)
    columns = analyze_columns(analysis_rows)
    report["columns"] = columns
    roles = infer_roles(columns, analysis_rows)
    report["roles"] = roles
    if "id" in roles:
        idv = roles["id_value"]
        report["id_matches_requested"] = {
            "uid": idv == cfg.INSTRUMENT["uid"],
            "figi": idv == cfg.INSTRUMENT["figi"],
            "id_kind_detected": roles.get("id_kind"),
            "documented_name_discrepancy_note": (
                "документация расходится в имени первого поля (UID vs FIGI, "
                "план 1.2.6); факт: значения колонки сверены с обоими "
                "идентификаторами конфига"
            ),
        }
    if "utc" in roles:
        report["time_facts"] = time_facts(analysis_rows, roles, requested_year)
        report["duplicate_conflict_scan"] = duplicate_conflict_scan(
            analysis_rows, roles)
        report["day_session_stats"] = day_session_stats(analysis_rows, roles)
        report["gap_stats"] = gap_stats(analysis_rows, roles)
    report["numeric_facts"] = numeric_facts(analysis_rows, roles)

    # Сверка фактического порядка полей с документированным (план 1.2)
    ohlc = report.get("roles", {}).get("ohlc_mapping")
    if ohlc is not None:
        report["field_order_vs_documentation"] = {
            "documented": cfg.DOCUMENTED_FIELD_ORDER,
            "matches_documented_order": ohlc.get("matches_documented_order"),
            "chosen_mapping": ohlc.get("chosen"),
            "ambiguous": ohlc.get("ambiguous"),
            "consistent_rates": {k: v.get("consistent_rate")
                                 for k, v in ohlc.items()
                                 if isinstance(v, dict) and "consistent_rate" in v},
        }
    return report


def write_markdown_summary(report: dict, path: Path) -> None:
    lines = ["# Инспекция ZIP — сводка (этап 2А)", ""]
    if report.get("error"):
        lines.append(f"ОШИБКА: {report['error']}")
    else:
        zf = report.get("zip_facts", {})
        lines += [
            f"- ZIP: `{report['zip_facts']['zip_path']}`",
            f"- Размер: {zf.get('file_size')} байт; CRC OK: **{zf.get('crc_ok')}**",
            f"- Члены ZIP: {report.get('all_members')}",
            f"- Кодировка: {report.get('encoding')}",
            f"- Разделитель: {report.get('delimiter')}",
            f"- Заголовок в CSV: **{report.get('header_present')}** "
            f"(имена: {report.get('header_names')})",
            f"- Строк данных: {report.get('data_rows')}",
            "",
            "## Роли колонок (факт)",
            "",
        ]
        roles = report.get("roles", {})
        for role in ("id", "utc", "volume"):
            if role in roles:
                lines.append(f"- `{role}` = колонка {roles[role]}"
                             + (f" (kind={roles['id_kind']}, value={roles['id_value']})"
                                if role == "id" else ""))
        if "price_columns" in roles:
            lines.append(f"- `цены` = колонки {roles['price_columns']}")
        if report.get("field_order_vs_documentation"):
            f = report["field_order_vs_documentation"]
            lines.append(
                f"- Сверка порядка с документацией: "
                f"matches_documented_order=**{f['matches_documented_order']}**, "
                f"ambiguous={f['ambiguous']}, rates={f['consistent_rates']}"
            )
        if report.get("id_matches_requested"):
            m = report["id_matches_requested"]
            lines.append(
                f"- ID в строках == запрошенному: uid={m['uid']}, figi={m['figi']}"
            )
        tf = report.get("time_facts", {})
        if tf:
            lines += [
                "",
                "## Время",
                "",
                f"- распознано {tf.get('parsed')}/{tf.get('parsed', 0) + tf.get('unparsed', 0)} меток; "
                f"форматы: {dict(tf.get('formats', {}))}",
                f"- наивные/aware: {tf.get('naive_strings')}/{tf.get('tz_aware_strings')}",
                f"- выравнивание на начало минуты: {tf.get('second_zero_aligned')}, "
                f"не на начало: {tf.get('second_nonzero')}",
                f"- вне запрошенного года: {tf.get('out_of_year')}",
                f"- диапазон: {tf.get('min_ts')} .. {tf.get('max_ts')}",
            ]
        ds = report.get("day_session_stats", {})
        if ds:
            lines += [
                "",
                "## Сессии и TZ",
                "",
                f"- дней: {ds.get('day_count')}; гипотеза TZ: **{ds.get('tz_hypothesis_preferred')}** "
                f"(счётчики: {ds.get('tz_plausibility_counts')})",
                f"- первые дни: {[{d['date']: [d['first_utc_hyp'], d['last_utc_hyp']]} for d in ds.get('days_first50', [])[:5]]}",
            ]
        gs = report.get("gap_stats", {})
        if gs:
            lines += [
                "",
                "## Пропуски",
                "",
                f"- дней: {gs.get('days')}; пропуски внутри span (суммарно): "
                f"{gs.get('total_missing_within_spans')}",
                f"- худшие дни: {gs.get('top_missing_within_span')}",
            ]
        nf = report.get("numeric_facts", {})
        if "volume" in nf:
            v = nf["volume"]
            lines += [
                "",
                "## Объём",
                "",
                f"- count={v['count']}, min={v['min']}, max={v['max']}, sum={v['sum']}, "
                f"zero={v['zero_count']}; все неотрицательные: {v['all_nonneg']}",
                f"- гипотеза единиц: **{v['documented_unit_hypothesis']}** (документация); "
                f"эмпирически: {'проверено' if v['unit_verified'] else 'НЕ проверено'}",
            ]
        pr = nf.get("prices", {})
        if pr:
            lines.append(
                f"- знаков после запятой в ценах: {pr['decimal_places_distribution']}; "
                f"нераспознанных ячеек: {pr['unparseable_price_cells']}"
            )
    common.atomic_write_text(path, "\n".join(lines) + "\n")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Этап 2А: инспекция фактической структуры ZIP-архива"
    )
    parser.add_argument("--zip", required=True, help="путь к ZIP-архиву")
    parser.add_argument("--year", type=int, default=cfg.PROTOTYPE_YEAR)
    parser.add_argument("--sample", type=int, default=20000,
                        help="строк выборки для структурного анализа")
    parser.add_argument("--full", action="store_true",
                        help="анализировать все строки (для полного отчёта)")
    parser.add_argument("--synthetic", action="store_true",
                        help="пометить отчёт как анализ синтетического фиксёра")
    parser.add_argument("--label", default=None,
                        help="метка артефакта (в имени файлов отчёта)")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    zip_path = Path(args.zip)
    if not zip_path.exists():
        common.log(f"ZIP не найден: {zip_path}")
        return 2
    report = inspect(zip_path, args.year, args.sample, args.full)
    report["synthetic"] = bool(args.synthetic)
    if args.label:
        report["label"] = args.label
    cfg.ZIP_STRUCTURE_DIR.mkdir(parents=True, exist_ok=True)
    label = args.label or zip_path.stem
    stamp = common.utc_now().strftime("%Y%m%dT%H%M%SZ")
    json_path = cfg.ZIP_STRUCTURE_DIR / f"zip_structure_{label}_{stamp}.json"
    md_path = cfg.ZIP_STRUCTURE_DIR / f"zip_structure_{label}_{stamp}.md"
    common.atomic_write_json(json_path, report)
    write_markdown_summary(report, md_path)
    common.log(f"Отчёт: {json_path}")
    common.log(f"Сводка: {md_path}")
    if report.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    main()
