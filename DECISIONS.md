# DECISIONS.md

1. **Indexes.** Unique `employees(emp_code)` and unique `attendance_logs(emp_code, date)` give uniqueness. `attendance_logs(date desc, emp_code)` serves the attendance list and the leaderboard date scan. `attendance_logs(emp_code, punch_in desc)` finds the open record at punch-out. `employees(department, joined_on)` and `employees(joined_on)` feed the department summary and trend. I rejected an index on `status` alone: it is low selectivity and the date/employee indexes already narrow the scan.

2. **Punch-in race.** Both requests compute the same date and try `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.

3. **Ties.** Ranking uses `$setWindowFields` with `$rank`, so ties share a rank and the next is skipped. `limit` filters on `rank <= limit` afterwards, so a tie at the cutoff returns every tied employee, even if that exceeds `limit` rows.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end), then `$lookup`s that month's logs per employee. Someone with no logs still produces a row, so they are counted.

5. **100x data.** I would pre-aggregate daily per-employee and per-department totals, updated at punch-in/out/correction, so analytics read a small summary collection instead of scanning raw logs.
