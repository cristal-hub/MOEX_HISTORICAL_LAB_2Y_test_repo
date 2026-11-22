#!/usr/bin/env python3
# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А, скрипт 1/4
# fetch_archive_zip.py — загрузка ОДНОГО годового ZIP-архива
# минутных свечей через T-Invest History API (read-only GET).
#
# RESEARCH ONLY. READ ONLY. NO LIVE ORDERS. NO REAL MONEY.
# NO MASS DOWNLOAD: строго ОДИН логический запрос (один архив) на запуск.
# Физически запуск может выполнить до N GET-попыток (ограничено):
# len(HISTORY_ENDPOINTS) x MAX_HTTP_ATTEMPTS; повтор только при 429
# (уважение Retry-After); полный учёт — в мете http_requests_max_possible
# и http_requests_made. Это НЕ «один GET».
#
# Гарантии:
# - токен читается ТОЛЬКО из переменной окружения
#   (см. hal_config.TOKEN_VAR_NAME); значение никогда не печатается,
#   не логируется и не попадает в отчёты;
# - торговые методы API не используются (единственный вызов —
#   GET /history-data, рыночные данные);
# - NO OVERWRITE: существующий ZIP не перезаписывается (fail-closed);
# - запись — tmp+rename в том же каталоге (атомарность файла);
# - при отсутствии токена HTTP-запрос НЕ выполняется (fail-closed),
#   вместо него создаётся честная запись меты executed=false;
# - самодиагностика ZIP: размер тела == Content-Length, CRC всех членов;
# - сетевые тайм-ауты (TimeoutError / socket.timeout) перехватываются
#   штатно: НЕ маскируются под HTTP-статус, НЕ ретраятся, сбор меты
#   не прерывают; неполный .part текущего запуска удаляется точечно
#   (TIMEOUT HANDLING FIX, задание 3.1-3.4);
# - PART FILE CLEANUP AUDIT: принадлежность .part текущему запуску
#   доказывается точно — .part, существовавший до первой попытки ЭТОГО
#   запуска, не открывается (не усекается) и не удаляется; текущий запуск
#   в этом случае пишет в уникальный временный файл
#   <имя>.part.<UTC-stamp>.<pid>, удаляемый только по этому точному пути
#   при любом неуспешном завершении (включая HTTP 404 и ранние возвраты,
#   гарантируется try/finally). Итоговый ZIP и чужие файлы не затрагиваются.
# ============================================================

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import hal_common as common
import hal_config as cfg


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Этап 2А: загрузка одного годового ZIP (T-Invest History API)"
    )
    parser.add_argument("--year", type=int, default=cfg.PROTOTYPE_YEAR,
                        help=f"год архива (по умолчанию {cfg.PROTOTYPE_YEAR})")
    parser.add_argument("--endpoint", default=None,
                        help="явный URL эндпоинта; по умолчанию — перебор HISTORY_ENDPOINTS")
    parser.add_argument("--id-type", choices=["uid", "figi"], default="uid",
                        help="тип идентификатора для instrument_id (figi deprecated)")
    parser.add_argument("--id-value", default=None,
                        help="значение идентификатора; по умолчанию — из hal_config.INSTRUMENT")
    parser.add_argument("--probe-noauth", action="store_true",
                        help="выполнить контрольный GET БЕЗ Authorization (фиксация "
                             "поведения без токена; не заменяет реальную загрузку)")
    parser.add_argument("--allow-overwrite", action="store_true",
                        help="разрешить перезапись существующего ZIP (по умолчанию запрещена)")
    return parser


def build_request_url(endpoint: str, instrument_id: str, year: int) -> str:
    """Полный GET URL History API: endpoint + обязательные query-параметры.

    Параметры instrument_id (uid или figi) и year передаются СЕРВЕРУ
    именно в URL (urllib.parse.urlencode — корректное кодирование);
    токен в URL не попадает никогда (только заголовок Authorization,
    см. http_get_stream). TLS-проверка не отключается, метод — GET.
    """
    query = urllib.parse.urlencode({
        "instrument_id": instrument_id,
        "year": int(year),
    })
    return f"{endpoint}?{query}"


def http_get_stream(url: str, token: str | None, out_tmp: Path,
                    timeout: int) -> dict:
    """Один read-only GET по готовому URL (с query-параметрами).

    Тело пишется в out_tmp потоком. Возвращает факт-словарь
    (status/headers/size). Токен не логируется.
    Сетевой тайм-аут (TimeoutError / socket.timeout) возвращает
    структурированный результат: status=None, headers={},
    network_error + error_type="timeout" — без маскировки
    под HTTP-статус.
    """
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "*/*")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    size_downloaded = 0
    status = None
    headers = {}
    body_sha_ctx = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
            digest = hashlib_sha256()
            with open(out_tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    size_downloaded += len(chunk)
                    digest.update(chunk)
            body_sha_ctx = digest.hexdigest()
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
        elapsed = round(time.monotonic() - started, 3)
        error_body = b""
        try:
            error_body = exc.read(4096)
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": status,
            "headers": headers,
            "elapsed_sec": elapsed,
            "size_downloaded": size_downloaded,
            "error_body_prefix": error_body.decode("utf-8", "replace")[:200],
        }
    except urllib.error.URLError as exc:
        # Сетевые/TLS-ошибки фиксируются в мете как факт окружения:
        # процесс не падает и меты не теряет (ответ носителя не гарантирован)
        elapsed = round(time.monotonic() - started, 3)
        reason = str(getattr(exc, "reason", exc))[:200]
        cause_name = type(exc.__cause__).__name__ if exc.__cause__ \
            else type(exc).__name__
        return {
            "status": None,
            "headers": {},
            "elapsed_sec": elapsed,
            "size_downloaded": size_downloaded,
            "network_error": f"{cause_name}: {reason}",
        }
    except (TimeoutError, socket.timeout) as exc:
        # Задание 3.1: сетевой тайм-аут (в т.ч. «The read operation timed
        # out» при чтении тела) перехватывается штатно и НЕ маскируется
        # под HTTP-статус. socket.timeout может совпадать с TimeoutError
        # (алиас в Python >= 3.10) — кортеж покрывает оба написания.
        # Повтор здесь не выполняется: решение принимает run_fetch
        # (ветка error_type == "timeout", задание 3.2).
        elapsed = round(time.monotonic() - started, 3)
        return {
            "status": None,
            "headers": {},
            "elapsed_sec": elapsed,
            "size_downloaded": size_downloaded,
            "network_error": f"{type(exc).__name__}: {exc}",
            "error_type": "timeout",
        }
    elapsed = round(time.monotonic() - started, 3)
    return {
        "status": status,
        "headers": headers,
        "elapsed_sec": elapsed,
        "size_downloaded": size_downloaded,
        "body_sha256": body_sha_ctx,
    }


def hashlib_sha256():
    import hashlib

    return hashlib.sha256()


def verify_zip_integrity(path: Path) -> dict:
    """CRC и структура ZIP: zipfile.testzip() + перечень членов."""
    result = {"is_zip": False, "crc_ok": False, "members": [], "bad_member": None,
              "total_uncompressed": 0}
    try:
        with zipfile.ZipFile(path) as zf:
            result["is_zip"] = True
            result["members"] = [
                {"name": info.filename, "size": info.file_size,
                 "compressed": info.compress_size, "crc32": info.CRC}
                for info in zf.infolist()
            ]
            result["total_uncompressed"] = sum(m["size"] for m in result["members"])
            bad = zf.testzip()
            result["bad_member"] = bad
            result["crc_ok"] = bad is None
    except zipfile.BadZipFile as exc:
        result["error"] = f"BadZipFile: {exc}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def pick_endpoint(explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    return list(cfg.HISTORY_ENDPOINTS)


def cleanup_part_file(tmp_path: Path) -> dict:
    """Задание 3.4 + PART FILE CLEANUP AUDIT: удалить .part ТОЛЬКО текущего запуска.

    Принадлежность текущему запуску определяет ВЫЗЫВАЮЩИЙ (run_fetch):
    передаётся ровно тот путь, в который ЭТОТ запуск писал тело
    (final_path.name + '.part', а при обнаружении .part предыдущего
    запуска — уникальный <имя>.part.<UTC-stamp>.<pid>). .part,
    существовавший до начала запуска, имеет ДРУГОЙ путь и поэтому
    принципиально не может быть удалён этой функцией.

    Двойной предохранитель:
    - удаляется ровно один файл по точному пути (никакого glob/широкой
      очистки каталога);
    - имя обязано содержать '.part': итоговый ZIP (.zip) и любые
      посторонние имена через эту функцию удалить невозможно.
    """
    result = {"path": tmp_path.name, "removed": False, "skipped_reason": None}
    if ".part" not in tmp_path.name:
        # Защита от ошибки вызова: cleanup не имеет права трогать
        # итоговый ZIP или посторонний файл.
        result["skipped_reason"] = "guard: имя не содержит .part"
        return result
    if tmp_path.exists():
        tmp_path.unlink()
        result["removed"] = True
    return result


def run_fetch(args) -> dict:
    """Основная логика. Возвращает мету запуска (без секретов)."""
    year = int(args.year)
    id_type = args.id_type
    # instrument_id: явное --id-value или фактическое значение конфига
    # для выбранного типа идентификатора (uid | figi)
    id_value = args.id_value or cfg.INSTRUMENT[id_type]
    ticker = cfg.INSTRUMENT["ticker"]
    endpoints = pick_endpoint(args.endpoint)
    meta = {
        "script": "fetch_archive_zip.py",
        "stage": "2A",
        "created_utc": common.iso_utc(common.utc_now()),
        "mode": "RESEARCH_ONLY / READ_ONLY / NO_LIVE_ORDERS",
        "requested": {
            "ticker": ticker,
            "year": year,
            "instrument_id_type": id_type,
            "instrument_id_value": id_value,
            "id_provenance": cfg.INSTRUMENT["id_provenance"],
            "timeframe_source": cfg.PROTOTYPE_TIMEFRAME_SRC,
        },
        "endpoints_order": endpoints,
        "http_requests_max_possible": len(endpoints) * cfg.MAX_HTTP_ATTEMPTS,
        "http_requests_made": 0,
        "token_variable": cfg.TOKEN_VAR_NAME,
        "token_available": common.token_available(),
        "token_value_saved": False,
        "self_limit": cfg.SELF_LIMIT_ARCHIVE_RPM and {
            "archive_rpm": cfg.SELF_LIMIT_ARCHIVE_RPM,
            "marked_as": "self_limit (не лимит API)",
        },
        "executed": False,
        "attempts": [],
        "saved_zip": None,
        "status_final": None,
        "reason_not_executed": None,
    }
    limiter = common.SlidingWindowRateLimiter(cfg.SELF_LIMIT_ARCHIVE_RPM)
    meta["limiter_snapshot_before"] = limiter.snapshot()
    timeout_count = 0  # попытки с error_type=timeout (для причины незавершения)

    token = None
    if args.probe_noauth:
        # Контрольный GET без авторизации выполняется и без токена:
        # фиксирует поведение эндпоинта для неавторизованного запроса
        # (ожидаемо 401/403). Реальной загрузки при этом не происходит.
        token = None
    else:
        try:
            token = common.load_token()
        except RuntimeError as exc:
            # fail-closed: без токена из окружения реальный GET не выполняется
            meta["reason_not_executed"] = str(exc)
            return meta

    final_path = cfg.RAW_ZIPS_DIR / f"{ticker}_{year}_{id_type}.zip"
    meta["planned_target"] = str(final_path)
    if final_path.exists() and not args.allow_overwrite:
        meta["reason_not_executed"] = (
            f"NO_OVERWRITE: целевой файл уже существует ({final_path.name}); "
            "перезапись запрещена; повторная загрузка требует явного "
            "--allow-overwrite или удаления старого артефакта"
        )
        return meta

    tmp_path = cfg.RAW_ZIPS_DIR / (final_path.name + ".part")
    # Каталог назначения создаётся ДО попыток: .part пишется потоком,
    # и отсутствие каталога не должно срывать загрузку на первом байте
    cfg.RAW_ZIPS_DIR.mkdir(parents=True, exist_ok=True)

    # PART FILE CLEANUP AUDIT (P1): принадлежность .part определяется
    # ДО первой сетевой попытки. Файл с фиксированным именем, уже
    # существующий в этот момент, создан НЕ текущим запуском (например,
    # остался после жёстко прерванного запуска): он не открывается
    # (не усекается) и не удаляется — это факт окружения, а не мусор
    # текущего запуска. Чтобы принадлежность временного файла текущего
    # запуска оставалась доказуемой точно, в этом случае используется
    # УНИКАЛЬНОЕ имя <имя>.part.<UTC-stamp>.<pid>[.<n>]: у предыдущего
    # и текущего запусков разные пути, пересечение невозможно.
    part_preexisting = tmp_path.exists()
    meta["part_preexisting"] = part_preexisting
    meta["tmp_file"] = tmp_path.name
    if part_preexisting:
        stamp = common.utc_now().strftime("%Y%m%dT%H%M%SZ")
        unique_base = tmp_path.name + f".{stamp}.{os.getpid()}"
        candidate = cfg.RAW_ZIPS_DIR / unique_base
        rotation = 0
        while candidate.exists():
            rotation += 1
            candidate = cfg.RAW_ZIPS_DIR / f"{unique_base}.{rotation}"
        tmp_path = candidate
        meta["tmp_file"] = tmp_path.name
        meta["part_preexisting_note"] = (
            "обнаружен .part предыдущего запуска по фиксированному имени; "
            "он не открывался, не усекался и не удалялся; тело текущего "
            "запуска пишется в уникальный временный файл tmp_file"
        )

    saved_ok = False  # временный файл ЭТОГО запуска стал итоговым ZIP
    try:
        for endpoint in endpoints:
            for attempt in range(1, cfg.MAX_HTTP_ATTEMPTS + 1):
                wait = limiter.acquire()
                # P0: параметры year и instrument_id уходят СЕРВЕРУ в URL
                url = build_request_url(endpoint, id_value, year)
                attempt_meta = {
                    "endpoint": endpoint,
                    "url": url,
                    "request_method": "GET",
                    "request_query": {
                        "instrument_id": id_value,
                        "year": year,
                    },
                    "attempt": attempt,
                    "self_limit_wait_sec": round(wait, 2),
                    "probe_noauth": bool(args.probe_noauth),
                }
                meta["attempts"].append(attempt_meta)
                common.log(
                    f"GET {url} attempt={attempt}"
                    f"{ ' (NOAUTH probe)' if args.probe_noauth else '' }"
                )
                result = http_get_stream(
                    url,
                    None if args.probe_noauth else token,
                    tmp_path,
                    cfg.REQUEST_TIMEOUT_SEC,
                )
                attempt_meta.update({k: v for k, v in result.items() if k != "headers"})
                attempt_meta["headers_of_interest"] = {
                    h: result["headers"].get(h)
                    for h in ("Content-Type", "Content-Length", "Content-Disposition",
                              "Retry-After", "x-ratelimit-limit", "x-ratelimit-remaining",
                              "x-ratelimit-reset", "Date")
                    if result["headers"].get(h) is not None
                }
                status = result.get("status")
                common.log(f"HTTP {status}, downloaded={result.get('size_downloaded')} bytes")

                if status == 200:
                    if args.probe_noauth:
                        attempt_meta["marked_as"] = (
                            "unexpected 200 without auth (probe only, "
                            "ZIP не сохраняется)"
                        )
                        meta["http_requests_made"] = len(meta["attempts"])
                        break
                    content_length = result["headers"].get("Content-Length")
                    size = result.get("size_downloaded", 0)
                    if content_length is not None and int(content_length) != size:
                        attempt_meta["verify"] = "content_length_mismatch"
                        common.log("Content-Length != downloaded size — бракую загрузку")
                        break
                    integrity = verify_zip_integrity(tmp_path)
                    attempt_meta["zip_integrity"] = integrity
                    if not (integrity["is_zip"] and integrity["crc_ok"]):
                        common.log("ZIP-интегрити не пройден — бракую загрузку")
                        break
                    cfg.RAW_ZIPS_DIR.mkdir(parents=True, exist_ok=True)
                    if final_path.exists() and not args.allow_overwrite:
                        meta["reason_not_executed"] = "NO_OVERWRITE: файл появился во время загрузки"
                        meta["http_requests_made"] = len(meta["attempts"])
                        # Очистку .part выполняет finally (P2): единая
                        # гарантия для любого неуспешного завершения
                        return meta
                    tmp_path.replace(final_path)
                    saved_ok = True   # временный файл ЭТОГО запуска переименован
                    meta["executed"] = True
                    meta["saved_zip"] = str(final_path)
                    meta["sha256"] = common.sha256_file(final_path)
                    meta["status_final"] = 200
                    meta["http_requests_made"] = len(meta["attempts"])
                    return meta

                if status == 429:
                    retry_after = result["headers"].get("Retry-After")
                    try:
                        pause = float(retry_after) if retry_after is not None \
                            else cfg.DEFAULT_RETRY_AFTER_SEC
                    except ValueError:
                        pause = cfg.DEFAULT_RETRY_AFTER_SEC
                    pause = min(pause, cfg.RETRY_429_BACKOFF_CAP_SEC)
                    attempt_meta["pause_429_sec"] = pause
                    attempt_meta["marked_as"] = "429 -> уважение Retry-After (или 5с по скрипту-примеру)"
                    common.log(f"429: пауза {pause:.1f} c и повтор")
                    time.sleep(pause)
                    continue

                if status == 404:
                    # План 13.5: 404 — ожидаемое поведение «нет архива за год»,
                    # фиксируется фактом; статус-кандидат absent_404.
                    # P2: возврат идёт через finally — временный файл
                    # текущего запуска очищается и здесь (ранний возврат
                    # больше не пропускает очистку).
                    attempt_meta["marked_as"] = "404: кандидат absent_404 (ожидание, план 13.5)"
                    meta["status_final"] = 404
                    meta["reason_not_executed"] = (
                        "HTTP 404: архив за запрошенный год не найден "
                        "(кандидат absent_404; подтверждение фактом — задача 2А/2Б)"
                    )
                    meta["http_requests_made"] = len(meta["attempts"])
                    return meta

                if result.get("error_type") == "timeout":
                    # Задание 3.2: тайм-аут — НЕ HTTP-статус и НЕ повод для
                    # авторетрая: фиксируется понятный marked_as, повторов нет,
                    # переход к следующему эндпоинту по текущей архитектуре
                    # сетевых ошибок (как для прочих невозвратных исходов).
                    attempt_meta["marked_as"] = "timeout_non_retryable"
                    timeout_count += 1
                    common.log(
                        "Timeout: HTTP-статус не получен; авторетрай тайм-аутов "
                        "запрещён заданием (п. 3.2) — переход к следующему эндпоинту"
                    )
                    break

                # 401/403/5xx/прочее: фиксирование и следующий эндпоинт
                attempt_meta["marked_as"] = "non-retryable status"
                break

            if meta["executed"]:
                break

    finally:
        # PART FILE CLEANUP AUDIT (P2): очистка временного файла ТЕКУЩЕГО
        # запуска выполняется при ЛЮБОМ неуспешном завершении попыток:
        # ранний возврат по HTTP 404, неожиданный 200 в probe-режиме,
        # тайм-аут, невозвратный статус, исчерпание 429-попыток и даже
        # непредвиденное исключение. saved_ok=True означает, что временный
        # файл уже атомарно переименован в итоговый ZIP — удалять нечего
        # и нечего фиксировать (прежняя семантика успешного пути).
        # Удаляется ровно путь tmp_path текущего запуска: .part предыдущего
        # запуска (другое имя), итоговый ZIP и чужие файлы не затрагиваются.
        if not saved_ok:
            meta["part_file_cleanup"] = cleanup_part_file(tmp_path)

    if not meta["executed"] and meta["status_final"] != 404:
        if timeout_count:
            meta["reason_not_executed"] = (
                f"сетевой тайм-аут в {timeout_count} из {len(meta['attempts'])} "
                "попыток (error_type=timeout, HTTP-статус не получен); "
                "авторетрай тайм-аутов запрещён заданием (п. 3.2); "
                "повторная загрузка — новый запуск скрипта"
            )
        else:
            meta["reason_not_executed"] = meta.get("reason_not_executed") or \
                "все попытки завершились без 200 (см. attempts)"

    # P3: фактическое число выполненных GET-попыток (<= http_requests_max_possible)
    meta["http_requests_made"] = len(meta["attempts"])

    return meta


def main() -> int:
    args = build_arg_parser().parse_args()
    meta = run_fetch(args)
    cfg.HTTP_META_DIR.mkdir(parents=True, exist_ok=True)
    stamp = common.utc_now().strftime("%Y%m%dT%H%M%SZ")
    meta_path = cfg.HTTP_META_DIR / f"fetch_meta_{cfg.INSTRUMENT['ticker']}_{args.year}_{stamp}.json"
    common.atomic_write_json(meta_path, meta)
    common.log(json.dumps(meta, ensure_ascii=False, indent=2))
    if meta.get("executed"):
        common.log(f"OK: ZIP сохранён: {meta['saved_zip']}")
        return 0
    if meta.get("status_final") == 404:
        return 4  # absent_404
    if not meta.get("token_available"):
        return 3  # токен недоступен в окружении
    return 2   # прочая незавершённая загрузка


if __name__ == "__main__":
    sys.exit(main())
