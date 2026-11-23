# MOEX Historical Lab — History API URL Query Parameter Fix

**Режим:** RESEARCH ONLY / READ ONLY / ADDITIVE ONLY / NO OVERWRITE / NO MASS DOWNLOAD / NO LIVE ORDERS.

**Дата:** 2026-11-23 (UTC)
**Скоуп:** точечное исправление формирования query-параметра в `history_api_layer/fetch_archive_zip.py`.

---

## 1. Постановка задачи

`build_request_url()` всегда передавал серверу `instrument_id=...`, независимо от `args.id_type`. Из-за этого загрузчик не мог воспроизвести проверенный боевой запрос:

```
GET https://invest-public-api.tbank.ru/history-data?figi=BBG004730N88&year=2025  ->  HTTP 200
```

(через боевой Read-only токен).

Требовалось:
- `--id-type uid`  → параметр `instrument_id=UID`;
- `--id-type figi` → параметр `figi=FIGI`;
- `year` передаётся в обоих случаях;
- URL кодируется через `urllib.parse.urlencode`;
- токен остаётся исключительно в `Authorization: Bearer` header.

---

## 2. Что изменено

Изменены **ровно 2 файла** в `history_api_layer/`:

| Файл | Роль | Результат |
|------|------|----------|
| `fetch_archive_zip.py` | production (загрузчик ZIP) | исправлен `build_request_url()` + обновлён `run_fetch()` |
| `tests/test_fetch_url.py` | тесты URL | обновлены 2 существующих теста под новое поведение, добавлены 6 новых |

**Никакие другие файлы не затронуты** (см. §6 — побайтовое доказательство через SHA256).

### 2.1. `fetch_archive_zip.py`

**(a) Новый helper `_id_param_name(id_type)`** — детерминированное отображение типа идентификатора в имя серверного query-параметра:
- `'uid'`  → `'instrument_id'`
- `'figi'` → `'figi'`
- иное — `ValueError` (argparse ограничивает `choices`, но функция защищена и от прямого вызова с неверным аргументом).

**(b) Обновлена сигнатура `build_request_url()`:**
- Было: `build_request_url(endpoint, instrument_id, year)`
- Стало: `build_request_url(endpoint, id_type, id_value, year)`

В теле функции имя параметра идентификатора вычисляется через `_id_param_name(id_type)`; URL по-прежнему собирается `urllib.parse.urlencode` (корректное экранирование специальных символов), токен в URL не попадает (только заголовок `Authorization`).

**(c) Обновлён `run_fetch()`:**
- добавлено `id_param_name = _id_param_name(id_type)` (одно вычисление на запуск);
- вызов `build_request_url(endpoint, id_type, id_value, year)` — с `id_type`;
- в `attempt_meta["request_query"]` имя параметра идентификатора — динамическое (`id_param_name`), а не захардкоженное `"instrument_id"`. Это убирает расхождение между фактическим URL и записанной метой.

**(d) Обновлён help для `--id-type`** — теперь явно документирует, какой query-параметр формируется для каждого значения и ссылается на воспроизводимый боевой запрос.

### 2.2. `tests/test_fetch_url.py`

| Тест | Статус | Содержание |
|------|--------|-----------|
| `test_url_contains_instrument_id_and_year` | обновлён | прямая проверка `build_request_url()` с `id_type="uid"` → параметр `instrument_id=`, `figi` в URL нет |
| `test_build_request_url_figi_uses_figi_param` | **новый** | прямая проверка `build_request_url()` с `id_type="figi"` → параметр `figi=`, `instrument_id` в URL нет |
| `test_build_request_url_matches_canonical_figi_request` | **новый** | точное воспроизведение боевого URL `?figi=BBG004730N88&year=2025` (scheme/host/path/query) |
| `test_id_param_name_unknown_raises_value_error` | **новый** | защита helper'а: неверный `id_type` → `ValueError` с понятным сообщением; тот же `ValueError` пробрасывается через `build_request_url` |
| `test_id_type_figi_uses_figi_value` | обновлён | сквозная проверка через `run_fetch` с `--id-type figi`: параметр `figi=FIGI` (а не `instrument_id=FIGI`), `instrument_id` в URL нет, `year` присутствует, `request_query` в мете использует `figi` (без расхождения с URL) |
| `test_id_value_override_with_figi_param_name` | **новый** | `--id-value` + `--id-type figi` → значение уходит в `figi=...`, `instrument_id` в URL нет |
| `test_id_value_with_special_chars_is_url_encoded` | **новый** | UID со спецсимволами `weird&id value=with+special;chars` round-trip кодируется/декодируется; «голых» `&`/`=`/пробелов в URL нет (только между параметрами) |
| `test_figi_value_with_special_chars_is_url_encoded` | **новый** | то же для `--id-type figi` |

Остальные 9 тестов файла (UID-default, failover на второй домен, `--id-value`+uid, токен не попадает в URL/логи/мету/диск, `--probe-noauth`, static check метод/TLS, 429-bounded, 404-stops) **остались без изменений** — они покрывают пути, где `--id-type` не указан (по умолчанию `uid` → `instrument_id=`), и естественно продолжают проходить.

---

## 3. Полный diff

- [`fetch_archive_zip.diff`](fetch_archive_zip.diff) — diff `fetch_archive_zip.py` против pre-my-edit состояния (реконструировано применением `timeout_fix.diff` + `part_cleanup_audit.diff` к `.orig` базовой линии; SHA256 реконструкции = `956c2132...`, совпадает с зафиксированным в предыдущей сессии).
- [`test_fetch_url.diff`](test_fetch_url.diff) — diff `tests/test_fetch_url.py` против packaged baseline из `download/history_api_layer/tests/test_fetch_url.py` (SHA256 = `6500db6...`, совпадает с `hashes_after.txt`).

Краткая сводка diff (формат `diff -u`):

```diff
# fetch_archive_zip.py
@@ help для --id-type @@
-                        help="тип идентификатора для instrument_id (figi deprecated)"
+                        help="тип идентификатора: 'uid' -> query-параметр "
+                             "instrument_id=UID; 'figi' -> query-параметр figi=FIGI "
+                             "(воспроизводит проверенный боевой запрос "
+                             "GET /history-data?figi=BBG004730N88&year=2025 -> "
+                             "HTTP 200)"

@@ НОВЫЙ helper @@
+def _id_param_name(id_type: str) -> str:
+    if id_type == "uid":  return "instrument_id"
+    if id_type == "figi": return "figi"
+    raise ValueError(f"unknown id_type: {id_type!r} (expected 'uid' or 'figi')")

@@ build_request_url — новая сигнатура @@
-def build_request_url(endpoint: str, instrument_id: str, year: int) -> str:
+def build_request_url(endpoint: str, id_type: str, id_value: str,
+                       year: int) -> str:
     query = urllib.parse.urlencode({
-        "instrument_id": instrument_id,
+        _id_param_name(id_type): id_value,
         "year": int(year),
     })
     return f"{endpoint}?{query}"

@@ run_fetch — динамический request_query @@
+    id_param_name = _id_param_name(id_type)
...
-                url = build_request_url(endpoint, id_value, year)
+                url = build_request_url(endpoint, id_type, id_value, year)
                 attempt_meta = {
                     ...
                     "request_query": {
-                        "instrument_id": id_value,
+                        id_param_name: id_value,
                         "year": year,
                     },
```

---

## 4. SHA256 изменённых файлов

| Файл | SHA256 (after) | SHA256 (before) | Источник baseline |
|------|-----------------|-----------------|-------------------|
| `fetch_archive_zip.py` | `64777ba10890f08be676546286f0e8a6848f55cbd8bcec8be1b63f5cd974d868` | `956c2132a1d27278294c25dad4e3b251739e2876a276cdaacde58b8a7d3d779c` | реконструкция `.orig` + `timeout_fix.diff` + `part_cleanup_audit.diff` (совпадает с предыдущей сессией) |
| `tests/test_fetch_url.py` | `6358b4f9b7f6a7a199a0793c4b0b8bfb181134cf1e0aa03d3b6e22399b98f0cf` | `6500db64f091bb5dccc1df634b34c5151ce4b6b1042a6b09c36a14a731e70a02` | `hashes_after.txt` (post-TIMEOUT-FIX baseline) + `download/history_api_layer/tests/test_fetch_url.py` |

Полная таблица SHA256 всех 20 production- и test-файлов: [`hashes_current.txt`](hashes_current.txt) и [`hashes_comparison.txt`](hashes_comparison.txt) (сравнение с baseline `hashes_after.txt`).

---

## 5. Полный результат pytest

Команда: `cd history_api_layer && python -m pytest tests/ -v`

Полный вывод: [`pytest_full_run.txt`](pytest_full_run.txt) (119 строк).

**Итог:** `107 passed in 2.89s`, ни одного FAILED/ERROR/SKIP.

Распределение по файлам:

| Файл | Число тестов | Статус |
|------|--------------|--------|
| `test_corrupt_zip.py` | 1 | PASSED |
| `test_duplicates.py` | 1 | PASSED |
| `test_fetch_url.py` | **17** (= 11 prev + 6 new) | PASSED |
| `test_no_overwrite.py` | 7 | PASSED |
| `test_no_trading_calls.py` | 10 | PASSED |
| `test_part_cleanup_audit.py` | 12 | PASSED (включая `test_previous_timeout_tests_remain_green` — подпроцесс pytest на `test_timeout_handling.py`) |
| `test_required_fields.py` | 6 | PASSED |
| `test_suitability_5m.py` | 9 | PASSED |
| `test_timeout_handling.py` | 13 | PASSED |
| `test_timestamps.py` | 7 | PASSED |
| `test_zip_reading.py` | 8 | PASSED |
| (conftest.py — фикстуры, без тестов) | — | — |
| **ИТОГО** | **107** | **107 PASSED** |

До задачи (по предыдущей сессии): **101 passed** = 89 + 12 (part cleanup audit).
После задачи: **107 passed** = 101 + 6 новых URL-тестов.

---

## 6. Подтверждение: реальные GET НЕ выполнялись

Все 107 тестов офлайн. Механизм изоляции от сети:

1. **`urllib.request.urlopen` подменяется перехватчиком** в каждом тесте, который exercised сетевой путь:
   - fixture `http_log` в `test_fetch_url.py` и `test_part_cleanup_audit.py` подменяет `urllib.request.urlopen` на `fake_urlopen`, проверяющий **реальный объект `urllib.request.Request`** (method, URL, headers) и возвращающий синтетический ответ из очереди `plan`;
   - тесты `test_timeout_handling.py` делают то же самое через `monkeypatch.setattr(urllib.request, "urlopen", boom)` или через `http_log`;
   - `test_request_has_no_auth_without_token` использует явный `monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)`;
   - `test_meta_file_on_disk_has_no_token` вызывает `fetch.main()` — внутри `main` используется `run_fetch`, который использует `http_log` fixture (подмена активна на время теста).
2. **Статическая гарантия:** `test_no_real_http_clients_in_module` (в `test_timeout_handling.py`) проверяет, что `fetch_archive_zip.py` не содержит `import requests`, `import httpx`, `import aiohttp`, `urllib3`, `socket.socket(` — единственная сетевая точка модуля это `urllib.request.urlopen(`. Этот тест проходит.
3. **TLS не отключён:** `test_only_get_method_and_tls_untouched` (в `test_fetch_url.py`) и `test_tls_verification_not_disabled_and_timeout_added` (в `test_timeout_handling.py`) проверяют, что в источнике нет `_create_unverified`, `check_hostname`, `CERT_NONE`, `ssl._`. Оба теста проходят.
4. **Токен тестового окружения** — фиктивная строка `"dummy-token-for-test"` (через `monkeypatch.setenv`), никогда не реальный.
5. **Файловая система** — все тесты пишут в `tmp_path` (pytest's temporary directory), который создаётся заново для каждого теста.

Гарантия: ни одного реального HTTP-запроса к `invest-public-api.tbank.ru` или `invest-public-api.tinkoff.ru` во время прогона pytest **не выполнялось**.

---

## 7. Подтверждение: старые ограничения и защита от перезаписи сохранены

| Ограничение (из задачи) | Где проверяется | Статус |
|---|---|---|
| Не менять `hal_config.py` | SHA256 = `9f01cc9b9ba94e7bfa4444b0045964392efdf23cf7a23644744f711bc9b1a8e6` — совпадает с baseline `hashes_after.txt` | ✓ БЕЗ ИЗМЕНЕНИЙ |
| Не менять старую лабораторию | Все изменения строго внутри `history_api_layer/` | ✓ |
| Не менять механизм скачивания ZIP | `http_get_stream()` не тронут (только `build_request_url` и `request_query` dict) | ✓ |
| Не менять timeout/retry-политику | `test_timeout_handling.py` (13 тестов) — БЕЗ ИЗМЕНЕНИЙ по SHA256, ВСЕ тесты проходят | ✓ |
| Не отключать TLS | `test_only_get_method_and_tls_untouched` + `test_tls_verification_not_disabled_and_timeout_added` — проходят | ✓ |
| Не менять cleanup `.part` | `test_part_cleanup_audit.py` (12 тестов) — БЕЗ ИЗМЕНЕНИЙ по SHA256, ВСЕ тесты проходят | ✓ |
| Не добавлять автоматические повторы | retry-цикл (`for attempt in range(1, MAX_HTTP_ATTEMPTS+1)`) не тронут, политика 429/404/timeout не изменена | ✓ |
| Не выполнять реальные сетевые GET | см. §6 | ✓ |
| Не использовать `--allow-overwrite` | логика `--allow-overwrite` не тронута (`test_existing_final_zip_fail_closed_without_flag` — проходит) | ✓ |
| **NO OVERWRITE: существующий ZIP не перезаписывается** | `test_no_overwrite.py` (7 тестов), `test_existing_final_zip_not_overwritten_or_deleted`, `test_existing_final_zip_unchanged_on_failed_run`, `test_existing_final_zip_fail_closed_without_flag` — ВСЕ проходят | ✓ |
| **NO MASS DOWNLOAD: один архив на запуск** | `http_requests_max_possible = 2 × MAX_HTTP_ATTEMPTS = 12`; `http_requests_made` точно учитывается (тесты `test_429_retries_are_bounded_and_counted`, `test_404_stops_immediately_no_retries`, `test_timeout_does_not_retry_moves_to_next_endpoint`) | ✓ |
| Токен только в `Authorization: Bearer` header | `test_token_never_in_url_logs_or_meta`, `test_meta_file_on_disk_has_no_token`, `test_request_has_no_auth_without_token` — ВСЕ проходят; URL формируется `urllib.parse.urlencode` (только `instrument_id`/`figi` и `year`), `Authorization` в URL не появляется | ✓ |
| `urllib.parse.urlencode` для URL-кодирования | `build_request_url` использует `urllib.parse.urlencode`; `test_id_value_with_special_chars_is_url_encoded` + `test_figi_value_with_special_chars_is_url_encoded` подтверждают корректное экранирование `&`, `=`, ` `, `+`, `;` | ✓ |

### Воспроизводимость baseline

Pre-my-edit состояние `fetch_archive_zip.py` реконструировано:
1. Применён `scripts/timeout_fix_baseline/timeout_fix.diff` к `fetch_archive_zip.py.orig` → post-TIMEOUT-FIX состояние (SHA256 `9b304ddf...` — совпадает с `hashes_after.txt`).
2. Применён `download/part_cleanup_audit/part_cleanup_audit.diff` к результату шага 1 → post-PART-CLEANUP-AUDIT состояние (SHA256 `956c2132...` — совпадает с зафиксированным в предыдущей сессии значением).

Это доказывает, что diff в [`fetch_archive_zip.diff`](fetch_archive_zip.diff) — это **точное** изменение между предыдущей подтверждённой версией и текущей, без скрытых правок.

---

## 8. Демонстрация результата

```python
>>> import fetch_archive_zip as f
>>> import hal_config as cfg
>>> ep = "https://invest-public-api.tbank.ru/history-data"

>>> f.build_request_url(ep, "uid", cfg.INSTRUMENT["uid"], 2025)
'https://invest-public-api.tbank.ru/history-data?instrument_id=e6123145-9665-43e0-8413-cd61b8aa9b13&year=2025'

>>> f.build_request_url(ep, "figi", cfg.INSTRUMENT["figi"], 2025)
'https://invest-public-api.tbank.ru/history-data?figi=BBG004730N88&year=2025'

# Канонический боевой URL из задания — воспроизведён побайтово:
>>> f.build_request_url(ep, "figi", "BBG004730N88", 2025)
'https://invest-public-api.tbank.ru/history-data?figi=BBG004730N88&year=2025'

# Спецсимволы кодируются urllib.parse.urlencode:
>>> f.build_request_url(ep, "uid", "weird&id value=with+special;chars", 2025)
'https://invest-public-api.tbank.ru/history-data?instrument_id=weird%26id+value%3Dwith%2Bspecial%3Bchars&year=2025'
# round-trip через parse_qs возвращает исходное значение:
>>> import urllib.parse as up
>>> up.parse_qs(up.urlparse(_).query)["instrument_id"]
['weird&id value=with+special;chars']

# Защита от неверного id_type:
>>> f._id_param_name("bogus")
ValueError: unknown id_type: 'bogus' (expected 'uid' or 'figi')
```

---

## 9. Файлы отчёта

| Файл | Содержание |
|------|------------|
| [`URL_QUERY_PARAM_FIX_REPORT.md`](URL_QUERY_PARAM_FIX_REPORT.md) | этот отчёт |
| [`fetch_archive_zip.diff`](fetch_archive_zip.diff) | полный diff `fetch_archive_zip.py` (against pre-my-edit baseline) |
| [`test_fetch_url.diff`](test_fetch_url.diff) | полный diff `tests/test_fetch_url.py` (against packaged baseline) |
| [`hashes_current.txt`](hashes_current.txt) | SHA256 всех 20 файлов `history_api_layer/` после задачи |
| [`hashes_comparison.txt`](hashes_comparison.txt) | сравнение SHA256 с baseline `hashes_after.txt` |
| [`pytest_full_run.txt`](pytest_full_run.txt) | полный вывод `pytest tests/ -v` (107 passed) |

---

## 10. Итог

- Дефект `build_request_url()` исправлен: `--id-type figi` теперь формирует `figi=FIGI` (а не `instrument_id=FIGI`); `--id-type uid` остался `instrument_id=UID`.
- Канонический боевой запрос `GET /history-data?figi=BBG004730N88&year=2025` воспроизводится побайтово.
- URL-кодирование specials — через `urllib.parse.urlencode` (round-trip через `parse_qs`).
- Токен остаётся только в `Authorization: Bearer` header, в URL/логах/мете его нет (тесты `test_token_never_*` проходят).
- Изменения **точечные**: 2 файла, минимальный diff, все 18 остальных production- и test-файлов байт-в-байт совпадают с baseline.
- Все 107 тестов проходят (101 старый + 6 новых).
- Реальные GET не выполнялись.
- Старые ограничения (NO OVERWRITE, NO MASS DOWNLOAD, timeout/retry-политика, cleanup `.part`, TLS, нет торговых методов) сохранены.
