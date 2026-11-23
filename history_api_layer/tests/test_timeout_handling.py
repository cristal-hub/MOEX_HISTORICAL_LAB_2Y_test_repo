# ============================================================
# Этап 2А, тесты исправления TIMEOUT HANDLING FIX (задание 3.1-3.4, п. 4)
#
# Проверяется:
# 1. TimeoutError -> структурированный результат, а не необработанное
#    исключение.
# 2. socket.timeout обрабатывается корректно (в Python >= 3.10 это
#    алиас TimeoutError; тест покрывает оба написания).
# 3. Тайм-аут не маскируется под HTTP-код (status=None).
# 4. Тайм-аут не вызывает retry: ровно одна попытка на эндпоинт,
#    переход к следующему по текущей архитектуре.
# 5. После тайм-аута метаданные сохраняются на диск (main()).
# 6. Два эндпоинта с тайм-аутами: executed=false, saved_zip=null,
#    причина незавершения, точные счётчики и время ожиданий.
# 7. Неполный .part текущего запуска удаляется.
# 8. Существующий итоговый ZIP не перезаписывается и не удаляется;
#    чужие файлы не затрагиваются (точечная очистка).
# 9. HTTP 429 обрабатывается прежним способом (retry сохранён).
# 10. HTTP 200 с корректным ZIP сохраняет архив и SHA256.
# 11. TLS-проверка не отключена (статическая проверка источника).
# 12. Тесты не выполняют реальных HTTP-запросов: все сетевые пути идут
#     через подменённый urllib.request.urlopen; сторонних HTTP-клиентов
#     в модуле нет.
#
# Сеть не используется. Токен — фиктивная строка тестового окружения.
# Production-файлы не изменяются (проверяется только fetch_archive_zip.py).
# ============================================================

import io
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import conftest as cf


class FakeResponse:
    """Минимальный ответ urlopen (контекстный менеджер, read(n))."""

    def __init__(self, status, data, headers=None):
        self.status = status
        self.data = data
        self.headers = headers or {}
        self._io = io.BytesIO(data)

    def read(self, n=-1):
        return self._io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PartialThenTimeoutResponse:
    """Ответ отдаёт один кусок тела, затем падает по TimeoutError.

    Имитирует обрыв чтения середины потока («The read operation timed
    out»): в .part остаётся частичное тело, которое обязано быть
    удалено после незавершённой загрузки (задание 3.4).
    """

    status = 200

    def __init__(self, first_chunk):
        self._chunk = first_chunk
        self._sent = False
        self.headers = {"Content-Type": "application/zip"}

    def read(self, n=-1):
        if not self._sent:
            self._sent = True
            return self._chunk
        raise TimeoutError("The read operation timed out")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _zip_bytes(n=6):
    return cf.zip_bytes(cf.make_rows(n))


@pytest.fixture()
def http_log():
    """Перехватчик urlopen: факты о каждом Request + очередь реакций."""
    log = {"requests": [], "plan": []}

    def fake_urlopen(request, timeout=None):
        log["requests"].append({
            "method": request.get_method(),
            "url": request.full_url,
            "has_auth": "Authorization" in request.headers,
        })
        if log["plan"]:
            action = log["plan"].pop(0)
        else:
            action = lambda req: FakeResponse(  # noqa: E731
                200, _zip_bytes(),
                {"Content-Type": "application/zip",
                 "Content-Length": str(len(_zip_bytes()))})
        return action(request)

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    yield log
    urllib.request.urlopen = original


@pytest.fixture()
def fetch_env(monkeypatch, tmp_path):
    import hal_config as cfg
    import fetch_archive_zip as fetch

    monkeypatch.setattr(cfg, "RAW_ZIPS_DIR", tmp_path / "raw")
    monkeypatch.setattr(cfg, "HTTP_META_DIR", tmp_path / "meta")
    monkeypatch.setenv("TINKOFF_READONLY_TOKEN", "dummy-token-for-test")
    return {"cfg": cfg, "fetch": fetch, "tmp": tmp_path}


def _timeout(_req):
    raise TimeoutError("The read operation timed out")


def _socket_timeout(_req):
    raise socket.timeout("timed out")


def _status429(_req):
    raise urllib.error.HTTPError(
        _req.full_url, 429, "Too Many Requests",
        {"Content-Type": "application/json", "Retry-After": "0"},
        io.BytesIO(b'{"error":"rate limit"}'))


def _ok_response(_req):
    data = _zip_bytes(6)
    return FakeResponse(200, data, {"Content-Type": "application/zip",
                                    "Content-Length": str(len(data))})


def _hosts(log):
    return [urllib.parse.urlparse(r["url"]).hostname for r in log["requests"]]


# ---------- 1: TimeoutError -> структурированный результат ----------

def test_timeouterror_returns_structured_result(fetch_env, monkeypatch):
    fetch = fetch_env["fetch"]

    def boom(request, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out_tmp = fetch_env["tmp"] / "out.zip.part"
    res = fetch.http_get_stream(
        "https://invest-public-api.tbank.ru/history-data"
        "?instrument_id=x&year=2025", "dummy-token-for-test", out_tmp, 30)
    assert isinstance(res, dict)                      # НЕ исключение
    assert res["status"] is None                      # статус не выдуман
    assert res["headers"] == {}
    assert res["error_type"] == "timeout"
    assert res["network_error"].startswith("TimeoutError:")
    assert "read operation timed out" in res["network_error"]
    assert res["size_downloaded"] == 0
    assert isinstance(res["elapsed_sec"], (int, float))
    assert res["elapsed_sec"] >= 0


# ---------- 2: socket.timeout ----------

def test_socket_timeout_returns_structured_result(fetch_env, monkeypatch):
    fetch = fetch_env["fetch"]

    def boom(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    res = fetch.http_get_stream(
        "https://invest-public-api.tinkoff.ru/history-data"
        "?instrument_id=x&year=2025", None, fetch_env["tmp"] / "out2.zip.part", 30)
    assert isinstance(res, dict)
    assert res["status"] is None
    assert res["error_type"] == "timeout"
    assert res["network_error"]                       # тип и текст ошибки
    assert "timeout" in res["network_error"].lower()


# ---------- 3: тайм-аут не маскируется под HTTP-код ----------

def test_timeout_not_masked_as_http_status(fetch_env, http_log):
    fetch = fetch_env["fetch"]
    http_log["plan"] = [_timeout, _socket_timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["status_final"] is None
    for a in meta["attempts"]:
        assert a["status"] is None                    # НЕ HTTP-код
        assert not isinstance(a["status"], int)
        assert a["error_type"] == "timeout"
        assert a["marked_as"] == "timeout_non_retryable"
        # не приписан чужой HTTP-смысл (429/5xx/прочее)
        assert "non-retryable status" != a["marked_as"]
        assert "429" not in a["marked_as"]


# ---------- 4: тайм-аут не вызывает retry ----------

def test_timeout_does_not_retry_moves_to_next_endpoint(fetch_env, http_log,
                                                       monkeypatch):
    fetch = fetch_env["fetch"]
    sleeps = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: sleeps.append(s))
    http_log["plan"] = [_timeout, _socket_timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    # ровно ОДНА попытка на каждый домен — без повторов в рамках домена
    assert _hosts(http_log) == ["invest-public-api.tbank.ru",
                                "invest-public-api.tinkoff.ru"]
    assert len(meta["attempts"]) == 2
    assert all(a["attempt"] == 1 for a in meta["attempts"])
    assert meta["http_requests_made"] == 2
    assert meta["http_requests_max_possible"] == \
        fetch_env["cfg"].MAX_HTTP_ATTEMPTS * len(fetch_env["cfg"].HISTORY_ENDPOINTS)
    # 429-паузы при тайм-ауте не применяются (нет ретрая)
    assert sleeps == []


# ---------- 5: после тайм-аута метаданные сохраняются ----------

def test_meta_saved_after_timeout(fetch_env, http_log, monkeypatch, capsys):
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    monkeypatch.setattr(sys, "argv", ["fetch_archive_zip.py"])
    http_log["plan"] = [_timeout, _socket_timeout]
    rc = fetch.main()
    capsys.readouterr()
    assert rc == 2   # прочая незавершённая загрузка (не падение)
    files = list(cfg.HTTP_META_DIR.glob("fetch_meta_*.json"))
    assert files, "мета-файл НЕ записан после тайм-аута"
    meta = json.loads(files[0].read_text(encoding="utf-8"))
    assert meta["executed"] is False
    assert meta["saved_zip"] is None
    assert meta["attempts"][0]["error_type"] == "timeout"
    assert meta["http_requests_made"] == 2
    assert "dummy-token-for-test" not in files[0].read_text(encoding="utf-8")


# ---------- 6: оба эндпоинта с тайм-аутами — полная мета ----------

def test_both_endpoints_timeout_full_meta(fetch_env, http_log):
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    http_log["plan"] = [_timeout, _socket_timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is False
    assert meta["saved_zip"] is None
    assert meta["status_final"] is None
    # оба эндпоинта и точное число фактических GET
    assert meta["endpoints_order"] == cfg.HISTORY_ENDPOINTS
    assert meta["http_requests_made"] == 2
    assert meta["http_requests_max_possible"] == cfg.MAX_HTTP_ATTEMPTS * 2
    # понятная причина незавершения
    assert "тайм-аут" in meta["reason_not_executed"]
    for a, wait_hint in zip(meta["attempts"],
                            ("invest-public-api.tbank.ru",
                             "invest-public-api.tinkoff.ru")):
        assert wait_hint in a["endpoint"]
        assert a["status"] is None
        assert a["error_type"] == "timeout"
        assert a["marked_as"] == "timeout_non_retryable"
        # время каждого ожидания фиксируется
        assert "self_limit_wait_sec" in a
        assert isinstance(a["elapsed_sec"], (int, float))
        assert "instrument_id=" in a["url"] and "year=2025" in a["url"]
    # токен не раскрыт
    assert "dummy-token-for-test" not in json.dumps(meta, ensure_ascii=False)
    assert "Bearer" not in json.dumps(meta, ensure_ascii=False)


# ---------- 7: неполный .part удаляется ----------

def test_partial_part_file_removed_after_timeout(fetch_env, http_log):
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    chunk = _zip_bytes(6)[:2048]          # частичное тело
    http_log["plan"] = [lambda req: PartialThenTimeoutResponse(chunk), _timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["attempts"][0]["size_downloaded"] == len(chunk)  # часть писалась
    assert meta["executed"] is False
    # .part текущего запуска удалён — незавершённых файлов нет
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []
    assert meta["part_file_cleanup"]["removed"] is True
    assert meta["part_file_cleanup"]["path"] == "SBER_2025_uid.zip.part"


# ---------- 8: существующий ZIP не перезаписывается и не удаляется ----------

def test_existing_final_zip_not_overwritten_or_deleted(fetch_env):
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    target = cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip"
    target.write_bytes(b"EXISTING-ARCHIVE-BYTES")
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is False
    assert meta["http_requests_made"] == 0        # fail-closed ДО сети
    assert "NO_OVERWRITE" in meta["reason_not_executed"]
    assert target.read_bytes() == b"EXISTING-ARCHIVE-BYTES"   # не тронут


def test_timeout_cleanup_keeps_unrelated_files(fetch_env, http_log):
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    unrelated = cfg.RAW_ZIPS_DIR / "SBER_2024_uid.zip"
    unrelated.write_bytes(b"OTHER-YEAR-ARCHIVE")
    http_log["plan"] = [_timeout, _socket_timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is False
    assert unrelated.read_bytes() == b"OTHER-YEAR-ARCHIVE"    # чужой файл цел
    assert not (cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip").exists()
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []        # только свой .part


# ---------- 9: HTTP 429 — прежний способ ----------

def test_429_retry_policy_unchanged(fetch_env, http_log, monkeypatch):
    fetch = fetch_env["fetch"]
    sleeps = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: sleeps.append(s))
    http_log["plan"] = [_status429, _ok_response]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    # retry при 429 сохранился: обе попытки на ОДНОМ домене
    assert meta["executed"] is True
    assert len(meta["attempts"]) == 2
    assert meta["attempts"][0]["status"] == 429
    assert meta["attempts"][1]["status"] == 200
    assert _hosts(http_log)[0] == _hosts(http_log)[1]
    assert meta["attempts"][0]["attempt"] == 1
    assert meta["attempts"][1]["attempt"] == 2
    assert sleeps == [0.0]   # уважение Retry-After: 0


# ---------- 10: HTTP 200 — прежний успешный путь ----------

def test_200_success_path_unchanged_saves_zip_and_sha256(fetch_env, http_log):
    import hal_common as common

    fetch = fetch_env["fetch"]
    http_log["plan"] = [_ok_response]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is True
    assert Path(meta["saved_zip"]).exists()
    assert meta["sha256"] == common.sha256_file(Path(meta["saved_zip"]))
    assert meta["attempts"][0]["zip_integrity"]["crc_ok"] is True
    assert meta["status_final"] == 200


# ---------- 11: TLS-проверка не отключена ----------

def test_tls_verification_not_disabled_and_timeout_added():
    src = (Path(__file__).resolve().parent.parent / "fetch_archive_zip.py") \
        .read_text(encoding="utf-8")
    # TLS не ослаблен (и не был ослаблен добавлением тайм-аутов)
    assert "_create_unverified" not in src
    assert "check_hostname" not in src
    assert "CERT_NONE" not in src
    assert "ssl._" not in src
    # добавленная обработка тайм-аутов действительно в источнике
    assert "except (TimeoutError, socket.timeout)" in src
    assert '"error_type": "timeout"' in src


# ---------- 12: тесты не выполняют реальных HTTP-запросов ----------

def test_no_real_http_clients_in_module():
    src = (Path(__file__).resolve().parent.parent / "fetch_archive_zip.py") \
        .read_text(encoding="utf-8")
    for fragment in ("import requests", "import httpx", "import aiohttp",
                     "urllib3", "socket.socket("):
        assert fragment not in src
    # единственная сетевая точка модуля — urllib.request.urlopen
    assert "urllib.request.urlopen(" in src
