#!/usr/bin/env python3
# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А, скрипт 1/4
# fetch_archive_zip.py — загрузка ОДНОГО годового ZIP-архива
# минутных свечей через T-Invest History API (read-only GET).
#
# RESEARCH ONLY. READ ONLY. NO LIVE ORDERS. NO REAL MONEY.
# NO BROKER EXECUTION. NO MASS DOWNLOAD (строго один запрос на запуск).
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
# - самодиагностика ZIP: размер тела == Content-Length, CRC всех членов.
# ============================================================

import argparse
import json
import sys
import time
import urllib.error
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


def http_get_stream(url: str, token: str | None, out_tmp: Path,
                    timeout: int) -> dict:
    """Один read-only GET. Тело пишется в out_tmp потоком.

    Возвращает факт-словарь (status/headers/size). Токен не логируется.
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


def run_fetch(args) -> dict:
    """Основная логика. Возвращает мету запуска (без секретов)."""
    year = int(args.year)
    id_type = args.id_type
    id_value = args.id_value or cfg.INSTRUMENT[id_type]
    ticker = cfg.INSTRUMENT["ticker"]
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
        "endpoints_order": pick_endpoint(args.endpoint),
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

    for endpoint in pick_endpoint(args.endpoint):
        for attempt in range(1, cfg.MAX_HTTP_ATTEMPTS + 1):
            wait = limiter.acquire()
            attempt_meta = {
                "endpoint": endpoint,
                "attempt": attempt,
                "self_limit_wait_sec": round(wait, 2),
                "probe_noauth": bool(args.probe_noauth),
            }
            meta["attempts"].append(attempt_meta)
            common.log(
                f"GET {endpoint} year={year} instrument_id={id_type} "
                f"attempt={attempt}{ ' (NOAUTH probe)' if args.probe_noauth else '' }"
            )
            result = http_get_stream(
                endpoint,
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
                    return meta
                tmp_path.replace(final_path)
                meta["executed"] = True
                meta["saved_zip"] = str(final_path)
                meta["sha256"] = common.sha256_file(final_path)
                meta["status_final"] = 200
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
                attempt_meta["marked_as"] = "404: кандидат absent_404 (ожидание, план 13.5)"
                meta["status_final"] = 404
                meta["reason_not_executed"] = (
                    "HTTP 404: архив за запрошенный год не найден "
                    "(кандидат absent_404; подтверждение фактом — задача 2А/2Б)"
                )
                return meta

            # 401/403/5xx/прочее: фиксирование и следующий эндпоинт
            attempt_meta["marked_as"] = "non-retryable status"
            break

        if meta["executed"]:
            break

    if not meta["executed"] and meta["status_final"] != 404:
        meta["reason_not_executed"] = meta.get("reason_not_executed") or \
            "все попытки завершились без 200 (см. attempts)"

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
