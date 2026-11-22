# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А: общие утилиты прототипа
# (history_api_layer/hal_common.py — новый файл, лабораторию не трогает)
#
# RESEARCH ONLY. READ ONLY. NO LIVE ORDERS. NO REAL MONEY.
# NO BROKER EXECUTION. ADDITIVE ONLY. NO OVERWRITE.
# ============================================================

import csv
import hashlib
import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


# ---------- Время ----------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def msk(dt: datetime) -> datetime:
    return dt.astimezone(timezone(timedelta(hours=3)))


# ---------- Токен (никогда не печатается и не возвращается в отчёты) ----------

def token_available() -> bool:
    """Проверяет наличие токена в окружении. Значение не раскрывается."""
    return bool(os.environ.get("TINKOFF_READONLY_TOKEN", "").strip())


def load_token() -> str:
    """Читает токен ТОЛЬКО из переменной окружения (безопасный источник).

    RuntimeError без раскрытия значения, если токен отсутствует/пуст.
    """
    value = os.environ.get("TINKOFF_READONLY_TOKEN", "").strip()
    if not value:
        raise RuntimeError(
            f"{TINKOFF_READONLY_TOKEN_NAME} отсутствует в окружении; "
            "реальный GET не выполняется (fail-closed)"
        )
    return value


TINKOFF_READONLY_TOKEN_NAME = "TINKOFF_READONLY_TOKEN"


# ---------- Атомарная запись (tmp+rename в том же каталоге; NO OVERWRITE) ----------

class NoOverwriteError(RuntimeError):
    """Попытка перезаписать существующий файл при overwrite=False."""


def atomic_write_bytes(path: Path, data: bytes, allow_overwrite: bool = False) -> None:
    """Атомарная запись байтов: tmp в том же каталоге + rename (replace-семантика).

    При allow_overwrite=False и существующем целевом файле — NoOverwriteError.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not allow_overwrite:
        raise NoOverwriteError(f"Файл уже существует, перезапись запрещена: {path}")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def atomic_write_text(path: Path, text: str, allow_overwrite: bool = False,
                      encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding), allow_overwrite=allow_overwrite)


def atomic_write_json(path: Path, payload, allow_overwrite: bool = False) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2),
        allow_overwrite=allow_overwrite,
    )


# ---------- Хэши ----------

def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- Self-limit rate limiter (sliding window, потокобезопасный) ----------

class SlidingWindowRateLimiter:
    """Скользящее окно на N запросов в минуту (self-limit лаборатории).

    НЕ является утверждением о лимитах API (план 1.4). Мьютекс обязателен
    (антипаттерн TKSBrokerAPI 2.4.1 не воспроизводится).
    """

    def __init__(self, rpm: int, window_sec: float = 60.0):
        self.rpm = int(rpm)
        self.window_sec = float(window_sec)
        self._events = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Блокирует до получения бюджета. Возвращает время ожидания (сек)."""
        with self._lock:
            now = time.monotonic()
            while self._events and now - self._events[0] >= self.window_sec:
                self._events.popleft()
            if len(self._events) < self.rpm:
                self._events.append(now)
                return 0.0
            wait = self.window_sec - (now - self._events[0])
        if wait > 0:
            time.sleep(wait)
        return self.acquire()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            active = sum(1 for t in self._events if now - t < self.window_sec)
            return {
                "self_limit_rpm": self.rpm,
                "active_in_window": active,
                "marked_as": "self_limit (не лимит API)",
            }


# ---------- Лог ----------

def log(message: str) -> None:
    print(message, flush=True)


# ---------- Парсинг времени (мультиформат, факт-ориентированный) ----------

_TIME_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y%m%d%H%M%S",
    "%d.%m.%Y %H:%M",
]


def parse_time_any(value: str):
    """Пытается распознать метку времени. Возвращает (datetime, label) или (None, None).

    datetime всегда naive-UTC-эквивалент (aware -> переводится в UTC и
    снимается tzinfo) — единое внутреннее представление. label — формат.
    """
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None

    # epoch seconds / millis
    if text.isdigit():
        digits = len(text)
        try:
            if digits == 10:
                return datetime.fromtimestamp(int(text), tz=timezone.utc).replace(
                    tzinfo=None
                ), "epoch_seconds"
            if digits == 13:
                return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc).replace(
                    tzinfo=None
                ), "epoch_millis"
        except (OverflowError, OSError, ValueError):
            return None, None

    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt, fmt
        except ValueError:
            continue

    # ISO-8601 (T/Z/offset, микросекунды)
    try:
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            label = "iso8601_offset"
        else:
            label = "iso8601_naive"
        return dt, label
    except ValueError:
        return None, None


def is_uuid_like(value: str) -> bool:
    text = str(value).strip().lower()
    if len(text) != 36 or text.count("-") != 4:
        return False
    parts = text.split("-")
    return all(len(p) == expect and all(c in "0123456789abcdef" for c in p)
               for p, expect in zip(parts, (8, 4, 4, 4, 12)))


def is_figi_like(value: str) -> bool:
    text = str(value).strip().upper()
    return (
        len(text) == 12
        and text.startswith("BBG")
        and all(c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in text)
    )


# ---------- Decimal-цены и валидация строки (план 9.2, 10.3) ----------

def parse_decimal_strict(value):
    """Decimal из исходной строки без float. (ok, value_or_None, reason)."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return False, None, "not_decimal"
    if not d.is_finite():
        return False, None, "not_finite"
    return True, d, ""


def validate_candle_row(open_d, close_d, high_d, low_d, volume_int):
    """Валидация одной свечи (план 9.2). Возвращает (ok, reasons[])."""
    reasons = []
    for name, d in (("open", open_d), ("close", close_d),
                    ("high", high_d), ("low", low_d)):
        if d is None:
            reasons.append(f"{name}_missing")
            continue
        if not d.is_finite() or math.isnan(float(d)) or math.isinf(float(d)):
            reasons.append(f"{name}_not_finite")
            continue
        if d <= 0:
            reasons.append(f"{name}_non_positive")
    if high_d is not None and open_d is not None and close_d is not None:
        if high_d < max(open_d, close_d):
            reasons.append("high_lt_max_open_close")
    if low_d is not None and open_d is not None and close_d is not None:
        if low_d > min(open_d, close_d):
            reasons.append("low_gt_min_open_close")
    if high_d is not None and low_d is not None and high_d < low_d:
        reasons.append("high_lt_low")
    if volume_int is None or volume_int < 0:
        reasons.append("volume_negative_or_missing")
    return (len(reasons) == 0), reasons


def parse_volume_int(value):
    try:
        text = str(value).strip()
        if not text or "." in text or "," in text or "e" in text.lower():
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


# ---------- CSV-чтение ----------

def iter_csv_rows(path_or_bytes_io, delimiter=",", encoding="utf-8"):
    """Потоковое чтение CSV-строк (списки значений)."""
    with path_or_bytes_io as f:
        reader = csv.reader(f, delimiter=delimiter)
        for row in reader:
            if row:
                yield row
