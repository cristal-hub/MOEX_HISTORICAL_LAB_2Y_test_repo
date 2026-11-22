# ============================================================
# Этап 2А, тесты: отсутствие торговых вызовов и безопасность токена
#
# Статический скан прототипа: только read-only GET /history-data,
# никаких торговых методов, никаких путей ордеров/счетов/позиций,
# токен читается только из переменной окружения и не печатается.
# ============================================================

import re
from pathlib import Path

import pytest

HAL_DIR = Path(__file__).resolve().parent.parent

PROTOTYPE_SOURCES = [
    p for p in HAL_DIR.glob("*.py") if p.name != "__init__.py"
]

FORBIDDEN_ENDPOINT_PATTERNS = [
    r"/orders",
    r"postorder",
    r"stoporder",
    r"sandbox",
    r"portfolio",
    r"positions",
    r"operations",
    r"/users",
    r"accounts",
    r"payin",
    r"payout",
    r"withdraw",
    r"ordersstream",
    r"grpc",
    r"wss://",
]

ALLOWED_HOSTS = ("invest-public-api.tbank.ru", "invest-public-api.tinkoff.ru")


def test_sources_exist():
    names = {p.name for p in PROTOTYPE_SOURCES}
    assert {
        "hal_config.py", "hal_common.py", "fetch_archive_zip.py",
        "inspect_zip.py", "normalize_ids_fields.py",
        "report_suitability_5m.py",
    } <= names


@pytest.mark.parametrize("path", PROTOTYPE_SOURCES, ids=lambda p: p.name)
def test_no_trading_endpoints(path):
    text = path.read_text(encoding="utf-8").lower()
    for pattern in FORBIDDEN_ENDPOINT_PATTERNS:
        assert not re.search(pattern, text), (
            f"{path.name}: найден запрещённый паттерн {pattern!r}"
        )


def test_http_method_is_get_only():
    text = (HAL_DIR / "fetch_archive_zip.py").read_text(encoding="utf-8")
    assert 'method="GET"' in text
    for verb in ("DELETE", "PATCH", "PUT", "POST"):
        assert not re.search(rf"method=[\"']{verb}[\"']", text)


def test_only_history_data_endpoint_used():
    text = (HAL_DIR / "fetch_archive_zip.py").read_text(encoding="utf-8")
    assert "history-data" in text
    hosts = set(re.findall(r"https://([a-z0-9.\-]+)/", text))
    for host in hosts:
        assert host in ALLOWED_HOSTS, f"внепротокольный хост: {host}"


def test_token_read_only_from_env_once():
    common_src = (HAL_DIR / "hal_common.py").read_text(encoding="utf-8")
    # Чтение токена — только через os.environ с именем переменной конфига
    assert "os.environ.get(" in common_src
    fetch_src = (HAL_DIR / "fetch_archive_zip.py").read_text(encoding="utf-8")
    # fetch не читает окружение напрямую — только через hal_common.load_token
    assert "os.environ" not in fetch_src


def test_token_value_never_logged_or_printed():
    for path in PROTOTYPE_SOURCES:
        for i, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if re.search(r"\b(print|log)\s*\(", line):
                # Строки вывода не должны содержать ни слово token в
                # свободном виде, ни "Bearer" (заголовок авторизации)
                assert "bearer" not in line.lower(), (path.name, i)
                assert not re.search(r"\btoken\b", line.lower()) or \
                    re.search(r"token_(available|variable|value_saved|state)", line.lower()), \
                    (path.name, i, line)


def test_authorization_header_construction_is_isolated():
    src = (HAL_DIR / "fetch_archive_zip.py").read_text(encoding="utf-8")
    # Заголовок строится ровно один раз (add_header) и не попадает в мету
    assert len(re.findall(r'add_header\("Authorization"', src)) == 1
    meta_part = src[src.find("meta = {"):]
    assert "Authorization" not in meta_part


def test_meta_never_contains_token_value():
    # Ключи меты содержат только факт наличия токена, не значение
    src = (HAL_DIR / "fetch_archive_zip.py").read_text(encoding="utf-8")
    assert '"token_value_saved": False' in src
    assert '"token_available"' in src


def test_rate_limiter_thread_safety():
    import threading

    import hal_common as common

    limiter = common.SlidingWindowRateLimiter(rpm=4, window_sec=1.0)
    results = []

    def worker():
        results.append(limiter.acquire())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snapshot = limiter.snapshot()
    assert snapshot["active_in_window"] == 4
