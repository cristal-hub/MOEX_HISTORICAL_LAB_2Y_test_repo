# ============================================================
# Этап 2А, тесты раунда ревью 1 (P0): формирование GET URL
#
# Требования задания:
# 1. instrument_id присутствует в ФАКТИЧЕСКОМ URL запроса.
# 2. В URL уходит ПРАВИЛЬНОЕ значение UID.
# 3. year присутствует в URL.
# 4. --id-type figi работает корректно.
# 5. --id-value работает корректно.
# 6. Токен отсутствует в URL, логах и метаданных.
# Дополнительно (P3): число HTTP-попыток ограничено и точно учитывается.
#
# Сеть не используется: urllib.request.urlopen подменяется перехватчиком,
# который проверяет РЕАЛЬНЫЙ объект urllib.request.Request (метод, URL,
# заголовки) и возвращает синтетический ответ.
# ============================================================

import io
import json
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


def _zip_bytes(n=6):
    return cf.zip_bytes(cf.make_rows(n))


@pytest.fixture()
def http_log():
    """Перехватчик urlopen: пишет факты о каждом Request в список."""
    log = {"requests": [], "plan": []}   # plan: очередь реакций (callable)

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


def _ok_response(_req):
    data = _zip_bytes(6)
    return FakeResponse(200, data, {"Content-Type": "application/zip",
                                    "Content-Length": str(len(data))})


def _status404(_req):
    raise urllib.error.HTTPError(
        _req.full_url, 404, "Not Found",
        {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"not found"}'))


def _status401(_req):
    raise urllib.error.HTTPError(
        _req.full_url, 401, "Unauthorized",
        {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"unauthorized"}'))


def _status429(_req):
    raise urllib.error.HTTPError(
        _req.full_url, 429, "Too Many Requests",
        {"Content-Type": "application/json", "Retry-After": "0"},
        io.BytesIO(b'{"error":"rate limit"}'))


def _query_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


# ---------- 1-3: instrument_id и year в фактическом URL ----------

def test_url_contains_instrument_id_and_year(fetch_env):
    fetch = fetch_env["fetch"]
    url = fetch.build_request_url(
        "https://invest-public-api.tbank.ru/history-data",
        fetch_env["cfg"].INSTRUMENT["uid"], 2025)
    parsed = urllib.parse.urlparse(url)
    assert parsed.path == "/history-data"
    q = urllib.parse.parse_qs(parsed.query)
    assert q["instrument_id"] == [fetch_env["cfg"].INSTRUMENT["uid"]]
    assert q["year"] == ["2025"]


def test_run_fetch_sends_uid_and_year_in_real_url(fetch_env, http_log):
    """Сквозная проверка: run_fetch передаёт URL с параметрами в http_get_stream."""
    fetch = fetch_env["fetch"]
    http_log["plan"] = [_ok_response]
    args = fetch.build_arg_parser().parse_args(["--year", "2025"])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is True
    assert len(http_log["requests"]) == 1
    req = http_log["requests"][0]
    assert req["method"] == "GET"
    q = _query_of(req["url"])
    assert q["instrument_id"] == [fetch_env["cfg"].INSTRUMENT["uid"]]
    assert q["year"] == ["2025"]
    # URL в мете совпадает с фактическим запросом
    assert meta["attempts"][0]["url"] == req["url"]


def test_run_fetch_falls_over_to_second_domain_with_params(fetch_env, http_log):
    """Оба официальных домена получают корректные query-параметры (P0).

    401 на первом домене — невозвратный статус (без повторов), переход
    ко второму; URL второго домена тоже обязан содержать параметры.
    (404 по плану 13.5 — немедленная фиксация absent_404 без перебора,
    см. test_404_stops_immediately_no_retries.)
    """
    fetch = fetch_env["fetch"]
    http_log["plan"] = [_status401, _ok_response]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is True
    assert len(http_log["requests"]) == 2
    hosts = [urllib.parse.urlparse(r["url"]).hostname for r in http_log["requests"]]
    assert hosts == ["invest-public-api.tbank.ru", "invest-public-api.tinkoff.ru"]
    for r in http_log["requests"]:
        q = _query_of(r["url"])
        assert q["instrument_id"] == [fetch_env["cfg"].INSTRUMENT["uid"]]
        assert q["year"] == ["2025"]
    # сохранённый ZIP — от второго домена
    assert Path(meta["saved_zip"]).exists()


# ---------- 4: --id-type figi ----------

def test_id_type_figi_uses_figi_value(fetch_env, http_log):
    fetch = fetch_env["fetch"]
    http_log["plan"] = [_ok_response]
    args = fetch.build_arg_parser().parse_args(["--id-type", "figi"])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is True
    assert meta["requested"]["instrument_id_type"] == "figi"
    assert meta["requested"]["instrument_id_value"] == \
        fetch_env["cfg"].INSTRUMENT["figi"]
    q = _query_of(http_log["requests"][0]["url"])
    assert q["instrument_id"] == [fetch_env["cfg"].INSTRUMENT["figi"]]


# ---------- 5: --id-value ----------

def test_id_value_override_in_url(fetch_env, http_log):
    fetch = fetch_env["fetch"]
    http_log["plan"] = [_ok_response]
    custom = "e6123145-9665-43e0-8413-cd61b8aa9b99"
    args = fetch.build_arg_parser().parse_args(
        ["--id-value", custom, "--id-type", "uid"])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is True
    assert meta["requested"]["instrument_id_value"] == custom
    q = _query_of(http_log["requests"][0]["url"])
    assert q["instrument_id"] == [custom]


# ---------- 6: токен не попадает в URL, логи и метаданные ----------

def test_token_never_in_url_logs_or_meta(fetch_env, http_log, capsys):
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    http_log["plan"] = [_ok_response]
    token = "dummy-token-for-test"
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    # URL
    for r in http_log["requests"]:
        assert token not in r["url"]
        assert "authorization" not in r["url"].lower()
        assert "bearer" not in r["url"].lower()
    # мета (полный JSON, как его пишет main)
    meta_json = json.dumps(meta, ensure_ascii=False)
    assert token not in meta_json
    assert "Bearer" not in meta_json
    assert "Authorization" not in meta_json
    # при этом заголовок авторизации реально передавался (перехватчик видел его),
    # но его значение не раскрыто ни в URL, ни в мете, ни в логах
    assert all(r["has_auth"] for r in http_log["requests"])

    # логи stdout
    out = capsys.readouterr().out
    assert token not in out
    assert "Bearer" not in out


def test_meta_file_on_disk_has_no_token(fetch_env, http_log, capsys, monkeypatch):
    import sys

    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    monkeypatch.setattr(sys, "argv", ["fetch_archive_zip.py"])
    http_log["plan"] = [_ok_response]
    rc = fetch.main()
    capsys.readouterr()
    assert rc == 0
    files = list(cfg.HTTP_META_DIR.glob("fetch_meta_*.json"))
    assert files, "мета-файл не записан"
    payload = files[0].read_text(encoding="utf-8")
    assert "dummy-token-for-test" not in payload
    assert "Bearer" not in payload
    assert "Authorization" not in payload
    # но URL с параметрами в мете есть
    meta = json.loads(payload)
    assert "instrument_id=" in meta["attempts"][0]["url"]
    assert "year=2025" in meta["attempts"][0]["url"]


def test_request_has_no_auth_without_token(monkeypatch, tmp_path):
    """--probe-noauth: запрос выполняется без заголовка Authorization."""
    import hal_config as cfg
    import fetch_archive_zip as fetch

    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append({"has_auth": "Authorization" in request.headers,
                         "url": request.full_url})
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized",
                                     {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cfg, "RAW_ZIPS_DIR", tmp_path / "raw")
    monkeypatch.delenv("TINKOFF_READONLY_TOKEN", raising=False)
    args = fetch.build_arg_parser().parse_args(["--probe-noauth"])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is False
    assert captured and captured[0]["has_auth"] is False
    assert "instrument_id=" in captured[0]["url"]


# ---------- статические гарантии метода/TLS ----------

def test_only_get_method_and_tls_untouched():
    src = (Path(__file__).resolve().parent.parent / "fetch_archive_zip.py") \
        .read_text(encoding="utf-8")
    assert 'Request(url, method="GET")' in src
    for verb in ("POST", "PUT", "DELETE", "PATCH"):
        assert f'method="{verb}"' not in src
    # TLS-проверка не отключается
    assert "_create_unverified" not in src
    assert "check_hostname" not in src
    assert "CERT_NONE" not in src
    assert "ssl._" not in src


# ---------- P3: ограниченность числа HTTP-вызовов ----------

def test_429_retries_are_bounded_and_counted(fetch_env, http_log, monkeypatch):
    """429 -> повтор в пределах MAX_HTTP_ATTEMPTS на домен; всего <= 2*MAX.

    Никакого бесконтрольного повторения: лимит исчерпывается,
    меты содержат точные счётчики http_requests_made/max_possible.
    """
    import fetch_archive_zip as fetch

    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)  # без реальных пауз
    # каждый вызов — 429 (план из 12 реакций: 2 домена x 6 попыток)
    http_log["plan"] = [_status429] * (fetch_env["cfg"].MAX_HTTP_ATTEMPTS * 2)

    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    expected = fetch_env["cfg"].MAX_HTTP_ATTEMPTS * 2   # 2 домена x 6 попыток
    assert meta["http_requests_max_possible"] == expected
    assert meta["http_requests_made"] == expected
    assert len(http_log["requests"]) == expected
    assert meta["executed"] is False
    assert all(a["status"] == 429 for a in meta["attempts"])
    # каждый URL — с параметрами
    for a in meta["attempts"]:
        assert "instrument_id=" in a["url"] and "year=2025" in a["url"]


def test_404_stops_immediately_no_retries(fetch_env, http_log):
    """404 — невозвратный статус: ОДНА попытка, без повторов и без второго домена."""
    fetch = fetch_env["fetch"]
    http_log["plan"] = [_status404]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["status_final"] == 404
    assert len(http_log["requests"]) == 1
    assert meta["http_requests_made"] == 1
    assert meta["attempts"][0]["attempt"] == 1
    assert "instrument_id=" in meta["attempts"][0]["url"]
