# REVIEW.md

Defects found in the starter `app/main.py`, how each shows up, and how it was fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health()` | Returns `ok` without touching MongoDB. | Stop MongoDB: `/health` still says 200. | Runs `admin.command("ping")`; any failure returns 503. |
| 2 | `create_employee()` | Check-then-insert: two parallel creates both pass `find_one`. No unique index existed. | 10 parallel POSTs with one `emp_code` give several 201s. | Unique index `emp_code` created at startup; `DuplicateKeyError` becomes 409. |
| 3 | `create_employee()` | `created_at = datetime.now()` is naive local time. Response returned a datetime, not epoch ms. | `created_at` differs by server time zone, wrong type. | UTC-aware instant, stored as BSON date, returned as epoch ms. |
| 4 | `EmployeeIn` | No validation: bad `emp_code`, bad email, bad dates/shift times, `shift_start == shift_end` all accepted. | POST `emp_code: "x"` or equal shifts gives 201. | Pydantic patterns/lengths, real date check, equal-shift validator (422). |
| 5 | `list_employees()` | `skip = page * page_size` skips a whole page. | `page=1` starts at record 21. | `skip = (page - 1) * page_size`. |
| 6 | `list_employees()` | `total = count_documents({})` ignores the department filter. | `?department=Sales` reports the whole company count. | Count uses the same filter as the page. |
| 7 | `list_employees()` | No sort and no bounds on `page`/`page_size`. | `page=0` gives a negative skip (500); `page_size=100000` allowed. | Sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100` (422). |
| 8 | `punch_in()` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises TypeError. | POST an unknown `emp_code` gives 500. | 404 before anything else. |
| 9 | `punch_in()` | `datetime.fromtimestamp(ms/1000)` is naive server-local time; `if body.punched_at` treats 0 as missing; floats, strings and seconds-looking values are accepted. | Run under a non-UTC TZ: date and late minutes shift. Send `1783312500` (seconds). | Strict int model, range 1e11..4.1e12 (422), UTC-aware conversion, `is not None` check. |
| 10 | `punch_in()` | Instant not truncated to whole seconds (R1). | `09:40:00.900` is stored with sub-seconds. | `ms // 1000` truncation before any calculation. |
| 11 | `punch_in()` | Attendance date taken from the (naive/local) timestamp, not IST, and no overnight rule. | 21:55 IST is 16:25 UTC; a UTC server could date it wrongly. 05:30 IST on a 22:00-06:00 shift should belong to the day before. | `attendance_date()` converts to IST and applies the overnight rule. |
| 12 | `punch_in()` | Check-then-insert race for (employee, date); no unique index. | Parallel identical punch-ins give duplicate records. | Unique compound index `(emp_code, date)`; `DuplicateKeyError` becomes 409. |
| 13 | `PunchInIn.status` | Any string accepted, including `ABSENT`/`LEAVE` or garbage. | POST `status: "FOO"` gives 201. | `Literal["PRESENT","WFH","ON_DUTY"]` (422). |
| 14 | `compute_late_minutes()` | Replaces hour/minute on the punch-in's own clock (UTC/naive, not IST), breaks overnight shifts, and truncates to whole minutes *before* the `> 10` test. | 09:40:01 returns 0 (should be 10); 09:40:30 returns 0. | Real shift-start instant; late only if `delta > 600 s`, then `delta // 60`. |
| 15 | `compute_work_hours()` | `round()` is banker's rounding on floats. | 3h 54s = 3.015 rounds to 3.01 instead of 3.02. | Exact `Decimal` arithmetic with `ROUND_HALF_UP`. |
| 16 | `compute_overtime()` | No 30-minute minimum; shift end built from the record date, so overnight shifts compare against the wrong day; naive/aware mix. | 5 minutes after shift end returns 5. Overnight 06:40 out returns a negative then 0. | `shift_bounds()` gives the real end instant (next day if overnight); floor minutes; counted only when >= 30. |
| 17 | `list_attendance()` | Loads the whole collection into Python, sorts in memory, paginates in Python. | Slow and memory-hungry at 100k records. | Filter, sort, skip, limit and `count_documents` all in MongoDB, with supporting indexes. |
| 18 | `list_attendance()` | Sort is `date` only (no `emp_code` tiebreak); no validation of dates, status, page bounds; `date_from > date_to` accepted. | Same-day rows come back in arbitrary order; bad dates silently match nothing. | Sort `(date desc, emp_code asc)`; strict date parsing, `Status` literal, page bounds, 422 on reversed range. |
| 19 | `list_attendance()` / `punch_in()` | Response leaks `id` (stringified `_id`) and raw datetimes instead of epoch ms; missing `half_day`/`history` on legacy docs. | Response contains `id` and ISO datetimes. | `serialize_attendance()`: no `_id`, epoch ms (including nested `history[].changes`), defaults for legacy gaps. |
| 20 | module setup | `MongoClient()` returns naive datetimes; no indexes at all; any DB error is a 500. | Comparing naive and aware datetimes raises; every query is a COLLSCAN. | `tz_aware=True`; idempotent `ensure_indexes()` at startup; `PyMongoError` handler returns 503. |

## Looked at and judged fine
- `load_dotenv()` does not override existing environment variables, so injected `MONGO_URI`/`MONGO_DB` win, as required.
- Plain `def` endpoints run in FastAPI's thread pool and the PyMongo client is thread-safe, so concurrent requests are fine.
- Default shifts `09:30`/`18:30` match the contract.
