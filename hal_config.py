# ============================================================
# MOEX_HISTORICAL_LAB_2Y — Этап 2А: изолированный прототип
# T-Invest History API (годовой ZIP-архив минутных свечей)
#
# RESEARCH ONLY. READ ONLY. NO LIVE ORDERS. NO REAL MONEY.
# NO BROKER EXECUTION. ADDITIVE ONLY. NO OVERWRITE. NO MASS DOWNLOAD.
#
# Этот каталог (history_api_layer/) — НОВЫЙ, независимый.
# Существующие файлы лаборатории не изменяются и не импортируются.
#
# Токен берётся ТОЛЬКО из предусмотренного безопасного источника
# окружения (переменная TOKEN_VAR_NAME). Значение токена никогда
# не печатается, не логируется и не сохраняется в отчёты.
# ============================================================

from pathlib import Path

# --- Прототип: инструмент и период (строго по заданию 2А) ---
PROTOTYPE_TICKER = "SBER"
PROTOTYPE_YEAR = 2025          # завершённый год, целиком в архиве
PROTOTYPE_TIMEFRAME_SRC = "1M"  # исходные данные архива — минутные свечи

# --- Идентификаторы инструмента ---
# Происхождение: локальный снимок InstrumentsDump.json проекта
# TKSBrokerAPI (тест-фикстура стороннего репозитория; дата снимка не
# зафиксирована). Реестр лаборатории results/instrument_registry/*.json
# в текущем снимке недоступен, поэтому значения помечены как
# «гайд-идентификаторы»: сверка по фактическому содержимому архива
# обязательна (нормализация проверяет ID-колонку против этих значений;
# при расхождении — STOP).
# FIGI задокументирован как deprecated для instrument_id (план 1.1);
# по умолчанию запрос выполняется instrument_id=uid.
INSTRUMENT = {
    "ticker": PROTOTYPE_TICKER,
    "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
    "figi": "BBG004730N88",
    "lot": 10,
    "min_price_increment": "0.01",
    "id_provenance": (
        "TKSBrokerAPI/tests/InstrumentsDump.json (локальный снимок, "
        "дата не зафиксирована); лабораторный реестр недоступен; "
        "требует сверки по фактическому содержимому архива"
    ),
}

# --- Endpoints History API ---
# Оба домена фигурируют в официальных источниках (план 1.1 / 13.6).
# Работающий домен фиксируется фактическим GET (этап 2А); порядок
# обхода — как в списке ниже. instrument_id принимает uid или figi.
HISTORY_ENDPOINTS = [
    "https://invest-public-api.tbank.ru/history-data",
    "https://invest-public-api.tinkoff.ru/history-data",
]

# --- Токен (безопасный источник окружения) ---
TOKEN_VAR_NAME = "TINKOFF_READONLY_TOKEN"

# --- Self-limits (СОБСТВЕННЫЕ ограничения лаборатории, НЕ лимиты API) ---
# План 1.4: self-limit архива 20/мин выбран консервативно относительно
# вторичного значения 30/мин; сам по себе ничего не утверждает о сервере.
SELF_LIMIT_ARCHIVE_RPM = 20

# --- HTTP-поведение ---
REQUEST_TIMEOUT_SEC = 30
MAX_HTTP_ATTEMPTS = 6
# Пауза по умолчанию при 429 без Retry-After: 5 с (поведение официального
# скрипта download_md.sh — вторичный источник, план 1.4).
DEFAULT_RETRY_AFTER_SEC = 5.0
RETRY_429_BACKOFF_CAP_SEC = 60.0

# --- Каталоги прототипа (все результаты — ТОЛЬКО внутри history_api_layer) ---
HAL_DIR = Path(__file__).resolve().parent
DATA_DIR = HAL_DIR / "data"
RAW_ZIPS_DIR = DATA_DIR / "raw_zips"
HTTP_META_DIR = DATA_DIR / "http_meta"
ZIP_STRUCTURE_DIR = DATA_DIR / "zip_structure"
NORMALIZED_DIR = DATA_DIR / "normalized"
REPORTS_DIR = DATA_DIR / "reports"
DEMO_FIXTURES_DIR = DATA_DIR / "demo_fixtures"

# --- Документированное описание архива (ТОЛЬКО для сравнения, НЕ для парсинга) ---
# План 1.2 (дословно из документации, проверено 2026-11-22):
# порядок полей UID, UTC, open, close, high, low, volume;
# UTC — дата и время НАЧАЛА свечи; volume — объём В ЛОТАХ;
# is_complete в архивном CSV в документации НЕ упоминается;
# имя первого поля — расхождение версий документации (UID vs FIGI) —
# факт фиксируется именно этим прототипом.
# Инспектор НЕ опирается на этот порядок как на допущение: он определяет
# фактическую структуру по содержимому и сверяет её с этой ссылкой.
DOCUMENTED_FIELD_ORDER = ["UID", "UTC", "open", "close", "high", "low", "volume"]
DOCUMENTED_VOLUME_UNIT = "lots"
DOCUMENTED_TIME_SEMANTICS = "UTC, начало минутного интервала"

# --- Порог нормализации (план 9.2.5) ---
MAX_REJECTED_ROW_SHARE = 0.005  # доля отбракованных строк > 0.5% года -> FAILED

# --- Проверка достоверности времени (эмпирическая) ---
# МСК = UTC+3 без сдвигов (план 8.1). Ориентировочные окна основной и
# вечерней сессий MOEX для эмпирической проверки гипотезы TZ у наивных
# меток времени. Это СПРАВОЧНЫЕ окна для вердикта правдоподобия, а не
# календарь торгов и не допущение при парсинге: инспектор считает
# распределение первых/последних минут дня и сравнивает две интерпретации.
MSK_UTC_OFFSET_HOURS = 3
SESSION_PLAUSIBILITY_MSK = {
    "main_open_earliest": "09:45",   # 09:50-09:59 аукцион открытия, 10:00 основная
    "main_close_typical": "18:45",
    "evening_open_earliest": "19:00",
    "evening_close_latest": "23:50",
}
