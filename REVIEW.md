# REVIEW.md — Starter Code Review

The following defects were identified in the supplied starter `app/main.py` and addressed in the submitted implementation.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returns 200 without checking MongoDB connectivity. | Stop MongoDB and call `GET /health`; starter still says `ok`. | Ping MongoDB and return 503 when unavailable. |
| 2 | `compute_late_minutes()` | Replaces the punch-in's time but retains its date and does not correctly model overnight shift attribution. | Test an overnight shift and punch-in after midnight. | Calculate the shift window from the record's attendance date and IST. |
| 3 | `compute_late_minutes()` | Uses `int()` on a potentially negative/positive float and its date handling makes edge boundaries fragile. | Test 09:40:00, 09:40:01, and 10:15:59 for a 09:30 shift. | Use whole-second elapsed time; apply the strict >600-second grace condition and floor whole minutes from shift start. |
| 4 | `compute_overtime()` | Builds a naive datetime then attaches IST and always assumes the shift ends on the attendance date. | Test 22:00–06:00 overnight shift with punch-out at 06:40 next day. | Build timezone-aware shift boundaries and advance the end date for overnight shifts. |
| 5 | `compute_work_hours()` | Python `round()` uses ties-to-even, not required half-up rounding. | Test a duration whose decimal hours is exactly at a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 6 | `EmployeeIn` | Employee code, email, shift formats, field lengths, and different shift start/end are not validated. | Submit a malformed employee or identical shift times. | Added contract-aligned Pydantic validation. |
| 7 | `create_employee()` | Duplicate check followed by insert is race-prone; simultaneous requests can both pass the check. | Send concurrent requests with the same `emp_code`. | Create a unique `emp_code` index and translate duplicate-key errors to 409. |
| 8 | `create_employee()` | Stores a naive `created_at` and does not return it in epoch milliseconds as required. | Create an employee and inspect `created_at`. | Store a UTC BSON datetime and serialize instants as integer epoch milliseconds. |
| 9 | `list_employees()` | `skip = page * page_size` skips the first page and `total` ignores the department filter. | Request page 1 with page size 2; filter by department and compare total. | Use `(page - 1) * page_size` and count the filtered query. |
| 10 | `punch_in()` | Missing employee is dereferenced rather than returning 404. | Punch in with an unknown `emp_code`; starter can raise 500. | Check employee existence and return 404. |
| 11 | `punch_in()` | Falsy timestamp handling, local-machine timezone, and missing strict epoch-millisecond validation are incorrect. | Send timestamp `0`, seconds, a float/string, or run under a different machine timezone. | Validate integer epoch milliseconds, truncate to whole seconds, use UTC instants and IST attendance dates. |
| 12 | `punch_in()` | Read-before-insert duplicate detection does not protect concurrent requests. | Send simultaneous punch-ins for one employee/date. | Enforce a unique compound `(emp_code, date)` index and handle duplicate-key errors as 409. |
| 13 | `punch_in()` | Returns an `id` derived from MongoDB `_id`, which violates the natural-key contract. | Inspect the punch-in response. | Remove all MongoDB identity fields from responses; records are addressed by `(emp_code, date)`. |
| 14 | `list_attendance()` | Loads all filtered documents into Python and sorts/slices in memory, which does not scale to 100k records. | Measure memory/time with a large collection. | Apply filters, sort, skip and limit in MongoDB and count the same filtered query. |
| 15 | `list_attendance()` | Does not validate date range order or status values. | Send `date_from > date_to` or an unsupported status. | Return 422 for invalid filters. |
| 16 | All endpoints / startup | No required application indexes are created. | Inspect `getIndexes()` on a clean database or inspect explain plans. | Create idempotent unique and query-supporting indexes on startup. |
| 17 | Missing endpoints | Punch-out, regularization, analytics, and explain endpoints are unimplemented. | Call the paths in `openapi.yaml`. | Implemented all required contract paths. |
| 18 | Serialization and stored defaults | Legacy records can omit `history` and `half_day`, and raw BSON datetimes are not contract-shaped epoch milliseconds. | Read a legacy record and inspect nested history times. | Normalize legacy defaults and convert all response instants, including audit changes, to epoch milliseconds. |

## Reviewed and retained

- `load_dotenv()` is appropriate: environment variables already set by the process take precedence over values in `.env`.
- MongoDB `_id` remains an internal storage key; application-facing attendance identity is `(emp_code, date)`.
- Derived attendance fields are computed by the API on writes and read from stored values in analytics, as required by the contract.
