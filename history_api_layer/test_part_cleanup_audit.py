# ============================================================
# Этап 2А, тесты PART FILE CLEANUP AUDIT (доработка timeout fix).
#
# Два замечания из ревью diff fetch_archive_zip.py:
# P1. cleanup_part_file() удалял файл по фиксированному имени без
#     подтверждения, что файл создан именно текущим запуском.
# P2. Ветка HTTP 404 возвращала meta до общего cleanup-блока: после
#     последовательности timeout (частичный .part) -> 404 временный
#     файл текущего запуска оставался на диске.
#
# Проверяется:
# 1. .part, существовавший ДО начала текущего запуска, не удаляется
#    и не усекается (запуск пишет в уникальное имя).
# 2. .part, созданный текущим запуском, удаляется при тайм-ауте.
# 3. .part, созданный текущим запуском, удаляется при последовательности
#    timeout -> HTTP 404 (регрессия P2).
# 4. Существующий итоговый ZIP не изменяется при неуспешном запуске.
# 5. Посторонние файлы в каталоге не затрагиваются.
# 6. Успешный HTTP 200 по-прежнему сохраняет ZIP и SHA256
#    (в т.ч. при наличии .part предыдущего запуска).
# 7. Все прежние timeout-тесты (tests/test_timeout_handling.py)
#    остаются зелёными — прогон того же файла в подпроцессе pytest.
#
# Дополнительно:
# 8. cleanup_part_file() принципиально не может удалить итоговый ZIP
#    (guard по имени).
# 9. Ранний возврат 404 без записи тела фиксирует cleanup-факт честно
#    (removed=false), ничего лишнего не удаляя.
# 10. Ротация уникального имени при уже занятых кандидатах.
#
# Тесты только офлайн: urllib.request.urlopen подменяется перехватчиком,
# реальных GET нет; сторонних HTTP-клиентов у модуля нет. Токен —
# фиктивная строка тестового окружения. hal_config.py не изменяется
# (каталоги подменяются monkeypatch на tmp_path).
# ============================================================

import io
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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

    Имитирует обрыв чтения середины потока: в .part остаётся частичное
    тело, созданное ТЕКУЩИМ запуском.
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


FIXED_PART_NAME = "SBER_2025_uid.zip.part"
PART_LEFTOVER_BYTES = b"PART-LEFTOVER-FROM-PREVIOUS-RUN"


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
            data = _zip_bytes()
            action = lambda req: FakeResponse(  # noqa: E731
                200, data, {"Content-Type": "application/zip",
                            "Content-Length": str(len(data))})
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


def _status404(_req):
    raise urllib.error.HTTPError(
        _req.full_url, 404, "Not Found",
        {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"not found"}'))


def _ok_response(_req):
    data = _zip_bytes(6)
    return FakeResponse(200, data, {"Content-Type": "application/zip",
                                    "Content-Length": str(len(data))})


def _partial_then_timeout(chunk):
    return lambda req: PartialThenTimeoutResponse(chunk)


def _hosts(log):
    return [urllib.parse.urlparse(r["url"]).hostname for r in log["requests"]]


# ---------- 1: существовавший до запуска .part не удаляется ----------

def test_preexisting_part_not_deleted_and_not_truncated(fetch_env, http_log):
    """P1: .part предыдущего запуска неприкосновенен.

    Файл по фиксированному имени, существующий ДО первой попытки,
    не удаляется и не усекается (не открывается вовсе): тело текущего
    запуска пишется в уникальное имя .part.<stamp>.<pid>.
    """
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    leftover = cfg.RAW_ZIPS_DIR / FIXED_PART_NAME
    leftover.write_bytes(PART_LEFTOVER_BYTES)

    chunk = _zip_bytes(6)[:2048]
    # попытка 1: частичное тело -> тайм-аут; попытка 2: тайм-аут
    http_log["plan"] = [_partial_then_timeout(chunk), _timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["executed"] is False
    # факт принадлежности зафиксирован честно
    assert meta["part_preexisting"] is True
    assert "part_preexisting_note" in meta
    assert meta["tmp_file"] != FIXED_PART_NAME
    assert meta["tmp_file"].startswith(FIXED_PART_NAME + ".")
    # файл предыдущего запуска НЕ удалён и байтово НЕ изменён
    # (не был усечён/перезаписан телом текущего запуска)
    assert leftover.exists()
    assert leftover.read_bytes() == PART_LEFTOVER_BYTES
    # временный файл ТЕКУЩЕГО запуска удалён по его точному пути
    assert meta["part_file_cleanup"]["path"] == meta["tmp_file"]
    assert meta["part_file_cleanup"]["removed"] is True
    # из *.part на диске остался только файл предыдущего запуска
    parts = sorted(p.name for p in cfg.RAW_ZIPS_DIR.glob("*.part"))
    assert parts == [FIXED_PART_NAME]
    assert (cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip").exists() is False


# ---------- 2: .part текущего запуска удаляется при тайм-ауте ----------

def test_current_run_part_removed_on_timeout(fetch_env, http_log):
    """Без предшественника текущий запуск использует фиксированное имя
    (обратная совместимость) и сам же его удаляет после тайм-аута."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    chunk = _zip_bytes(6)[:2048]
    http_log["plan"] = [_partial_then_timeout(chunk), _timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["executed"] is False
    assert meta["attempts"][0]["size_downloaded"] == len(chunk)  # часть писалась
    assert meta["part_preexisting"] is False
    assert meta["tmp_file"] == FIXED_PART_NAME
    assert meta["part_file_cleanup"]["removed"] is True
    assert meta["part_file_cleanup"]["path"] == FIXED_PART_NAME
    assert meta["part_file_cleanup"]["skipped_reason"] is None
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []


# ---------- 3: .part текущего запуска удаляется при timeout -> 404 ----------

def test_current_run_part_removed_on_timeout_then_404(fetch_env, http_log):
    """Регрессия P2: endpoint-1 частично пишет .part и падает по
    тайм-ауту, endpoint-2 отвечает HTTP 404 — ранний возврат больше
    НЕ пропускает очистку временного файла текущего запуска."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    chunk = _zip_bytes(6)[:2048]
    http_log["plan"] = [_partial_then_timeout(chunk), _status404]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    # последовательность зафиксирована в мете
    assert len(meta["attempts"]) == 2
    assert meta["attempts"][0]["error_type"] == "timeout"
    assert meta["attempts"][0]["size_downloaded"] == len(chunk)
    assert meta["attempts"][1]["status"] == 404
    assert meta["status_final"] == 404
    assert meta["executed"] is False
    assert meta["http_requests_made"] == 2
    # очистка ВЫПОЛНЕНА, несмотря на ранний возврат по 404
    assert meta["part_preexisting"] is False
    assert meta["tmp_file"] == FIXED_PART_NAME
    assert meta["part_file_cleanup"]["removed"] is True
    assert meta["part_file_cleanup"]["path"] == FIXED_PART_NAME
    # незавершённых временных файлов на диске нет
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []
    assert (cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip").exists() is False


# ---------- 4: существующий итоговый ZIP не изменяется ----------

def test_existing_final_zip_unchanged_on_failed_run(fetch_env, http_log):
    """Итоговый ZIP существует; запуск с --allow-overwrite терпит неудачу
    (частичное тело + тайм-аут) — ZIP байтово не изменяется, .part удалён."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    target = cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip"
    target.write_bytes(b"EXISTING-ARCHIVE-BYTES")

    chunk = _zip_bytes(6)[:2048]
    http_log["plan"] = [_partial_then_timeout(chunk), _timeout]
    args = fetch.build_arg_parser().parse_args(["--allow-overwrite"])
    meta = fetch.run_fetch(args)

    assert meta["executed"] is False
    assert meta["saved_zip"] is None
    assert target.read_bytes() == b"EXISTING-ARCHIVE-BYTES"   # не тронут
    assert meta["part_file_cleanup"]["removed"] is True       # свой .part убран
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []
    assert (cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip").exists() is True


def test_existing_final_zip_fail_closed_without_flag(fetch_env, http_log):
    """Без --allow-overwrite существующий ZIP -> fail-closed ДО сети:
    ни одного GET, никаких файлов, .part-фактов нет."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    target = cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip"
    target.write_bytes(b"EXISTING-ARCHIVE-BYTES")

    http_log["plan"] = [_timeout, _timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["http_requests_made"] == 0
    assert len(http_log["requests"]) == 0
    assert "NO_OVERWRITE" in meta["reason_not_executed"]
    assert target.read_bytes() == b"EXISTING-ARCHIVE-BYTES"
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []


# ---------- 5: посторонние файлы не затрагиваются ----------

def test_foreign_files_untouched(fetch_env, http_log):
    """Очистка точечная: чужой архив, чужая заметка и чужой .part с
    отличающимся именем остаются байтово неизменными; удаляется только
    временный файл текущего запуска по его точному пути."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    foreign_zip = cfg.RAW_ZIPS_DIR / "SBER_2024_uid.zip"
    foreign_zip.write_bytes(b"OTHER-YEAR-ARCHIVE")
    foreign_note = cfg.RAW_ZIPS_DIR / "notes.txt"
    foreign_note.write_bytes(b"human notes, not a part file")
    # чужой .part с другим именем: содержит ".part", но путь другой
    foreign_part = cfg.RAW_ZIPS_DIR / "OTHER_INSTRUMENT_2025_uid.zip.part"
    foreign_part.write_bytes(b"someone else's partial download")

    chunk = _zip_bytes(6)[:2048]
    http_log["plan"] = [_partial_then_timeout(chunk), _timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["executed"] is False
    assert foreign_zip.read_bytes() == b"OTHER-YEAR-ARCHIVE"
    assert foreign_note.read_bytes() == b"human notes, not a part file"
    assert foreign_part.read_bytes() == b"someone else's partial download"
    # удалён только временный файл текущего запуска
    assert meta["part_file_cleanup"]["removed"] is True
    assert meta["part_file_cleanup"]["path"] == FIXED_PART_NAME
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == [foreign_part]
    assert (cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip").exists() is False


# ---------- 6: HTTP 200 сохраняет ZIP и SHA256 (прежний путь) ----------

def test_200_success_saves_zip_and_sha256(fetch_env, http_log):
    import hal_common as common

    fetch = fetch_env["fetch"]
    http_log["plan"] = [_ok_response]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)
    assert meta["executed"] is True
    assert meta["status_final"] == 200
    assert Path(meta["saved_zip"]).exists()
    assert meta["sha256"] == common.sha256_file(Path(meta["saved_zip"]))
    assert meta["attempts"][0]["zip_integrity"]["crc_ok"] is True
    # успешный путь: временный файл переименован, cleanup не требуется
    assert "part_file_cleanup" not in meta


def test_200_success_with_preexisting_part_leftover_kept(fetch_env, http_log):
    """P1 + прежний успешный путь: при наличии .part предыдущего запуска
    200-загрузка пишется в уникальное имя, сохраняет ZIP и SHA256,
    а файл предыдущего запуска остаётся нетронутым."""
    import hal_common as common

    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    leftover = cfg.RAW_ZIPS_DIR / FIXED_PART_NAME
    leftover.write_bytes(PART_LEFTOVER_BYTES)

    http_log["plan"] = [_ok_response]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["executed"] is True
    assert meta["part_preexisting"] is True
    assert meta["tmp_file"] != FIXED_PART_NAME
    final_zip = cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip"
    assert final_zip.exists()
    assert meta["sha256"] == common.sha256_file(final_zip)
    # файл предыдущего запуска не удалён и не изменён
    assert leftover.exists()
    assert leftover.read_bytes() == PART_LEFTOVER_BYTES
    # уникальный временный файл текущего запуска переименован в ZIP
    assert meta["tmp_file"] not in [p.name for p in cfg.RAW_ZIPS_DIR.iterdir()]


# ---------- 7: прежние timeout-тесты остаются зелёными ----------

def test_previous_timeout_tests_remain_green():
    """tests/test_timeout_handling.py (13 тестов прежнего задания 3.1-3.4)
    прогоняется без изменений в подпроцессе pytest — офлайн, на моках."""
    tests_dir = Path(__file__).resolve().parent
    old_file = tests_dir / "test_timeout_handling.py"
    assert old_file.exists()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(old_file), "-q",
         "-p", "no:cacheprovider"],
        cwd=str(tests_dir.parent),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        "прежние timeout-тесты сломаны доработкой P1/P2:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


# ---------- 8: guard cleanup — итоговый ZIP удалить невозможно ----------

def test_cleanup_part_file_refuses_non_part_name(fetch_env):
    """Предохранитель cleanup_part_file: имя без '.part' (например,
    итоговый ZIP) отвергается — файл не удаляется, причина фиксируется."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    final_zip = cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip"
    final_zip.write_bytes(b"FINAL-ARCHIVE")

    result = fetch.cleanup_part_file(final_zip)
    assert result["removed"] is False
    assert result["path"] == "SBER_2025_uid.zip"
    assert result["skipped_reason"] == "guard: имя не содержит .part"
    assert final_zip.exists()
    assert final_zip.read_bytes() == b"FINAL-ARCHIVE"

    # постороннее имя тоже не трогается
    stray = cfg.RAW_ZIPS_DIR / "random.txt"
    stray.write_bytes(b"x")
    assert fetch.cleanup_part_file(stray)["removed"] is False
    assert stray.exists()


# ---------- 9: 404 без записи тела — честный cleanup-факт ----------

def test_404_first_endpoint_early_return_records_cleanup(fetch_env, http_log):
    """Ранний возврат 404 без единого записанного байта: cleanup-факт
    присутствует в мете (removed=false — удалять было нечего), дисковое
    состояние не меняется."""
    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    http_log["plan"] = [_status404]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["status_final"] == 404
    assert meta["executed"] is False
    assert meta["http_requests_made"] == 1
    assert meta["part_preexisting"] is False
    assert meta["tmp_file"] == FIXED_PART_NAME
    assert meta["part_file_cleanup"]["removed"] is False
    assert meta["part_file_cleanup"]["path"] == FIXED_PART_NAME
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == []
    assert (cfg.RAW_ZIPS_DIR / "SBER_2025_uid.zip").exists() is False


# ---------- 10: ротация уникального имени при занятых кандидатах ----------

def test_unique_tmp_name_rotation(fetch_env, http_log, monkeypatch):
    """Если даже первый кандидат уникального имени занят, запуск
    перебирает суффиксы .1, .2, ... — и трогает только СВОЙ путь."""
    import hal_common as common

    fetch, cfg = fetch_env["fetch"], fetch_env["cfg"]
    cfg.RAW_ZIPS_DIR.mkdir(parents=True)
    fixed_dt = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(common, "utc_now", lambda: fixed_dt)

    stamp = fixed_dt.strftime("%Y%m%dT%H%M%SZ")
    base = f"{FIXED_PART_NAME}.{stamp}.{os.getpid()}"
    # файл предыдущего запуска + два занятых кандидата
    (cfg.RAW_ZIPS_DIR / FIXED_PART_NAME).write_bytes(PART_LEFTOVER_BYTES)
    (cfg.RAW_ZIPS_DIR / base).write_bytes(b"occupied candidate 0")
    (cfg.RAW_ZIPS_DIR / f"{base}.1").write_bytes(b"occupied candidate 1")

    chunk = _zip_bytes(6)[:2048]
    http_log["plan"] = [_partial_then_timeout(chunk), _timeout]
    args = fetch.build_arg_parser().parse_args([])
    meta = fetch.run_fetch(args)

    assert meta["executed"] is False
    assert meta["part_preexisting"] is True
    assert meta["tmp_file"] == f"{base}.2"          # ротация сработала
    assert meta["part_file_cleanup"]["path"] == f"{base}.2"
    assert meta["part_file_cleanup"]["removed"] is True
    # ни занятые кандидаты, ни файл предыдущего запуска не тронуты
    assert (cfg.RAW_ZIPS_DIR / FIXED_PART_NAME).read_bytes() == \
        PART_LEFTOVER_BYTES
    assert (cfg.RAW_ZIPS_DIR / base).read_bytes() == b"occupied candidate 0"
    assert (cfg.RAW_ZIPS_DIR / f"{base}.1").read_bytes() == \
        b"occupied candidate 1"
    assert list(cfg.RAW_ZIPS_DIR.glob("*.part")) == [cfg.RAW_ZIPS_DIR / FIXED_PART_NAME]
