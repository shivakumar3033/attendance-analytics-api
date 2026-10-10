# DECISIONS.md

## Indexes
I create a unique index on `employees.emp_code` and a unique compound index on `attendance_logs(emp_code, date)`. These enforce identity and the one-record-per-employee-per-attendance-day invariant even under concurrent requests. Compound indexes on attendance date/employee and date/status/employee support the list and month analytics filters; an employee department/join-date index supports department filtering and headcount. I would confirm each with `executionStats` on the full dataset and remove indexes that do not support measured queries.

## Concurrent punch-ins
Both requests may attempt the insert, but the unique compound index is the final authority. One insert succeeds with 201; the duplicate-key failure is converted to 409. A read-before-insert check alone cannot guarantee this under concurrency.

## Leaderboard ties
MongoDB `$rank` gives equal total-late-minute values the same rank and skips the next rank (1, 2, 2, 4). I filter `rank <= limit` after ranking, then sort by total late minutes descending and employee code ascending, so ties at the cutoff are all included and deterministic.

## Department headcount
The summary starts with employees whose `joined_on` is on or before the month end, then looks up their attendance records. This includes eligible employees with no logs and excludes people who have not joined. Average work hours are computed across qualifying present-status records with non-null work hours, not by averaging employee averages.

## 100x scale
I would profile the real workload, verify index plans and aggregation memory, load-test concurrent writes, and add monitoring and operational alerts. I would consider partitioning or sharding only after measuring bottlenecks and selecting a shard key that matches the query patterns.
