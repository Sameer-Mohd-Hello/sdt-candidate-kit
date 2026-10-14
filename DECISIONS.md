# DECISIONS.md

1. !!Indexes.!! I created a unique index on "employees.emp_code" to guarantee a single employee record per business code, plus a departmental index to filter and group by department quickly. For attendance I created a unique index on "(emp_code, date)" so a given employee cannot have two records for the same attendance day. I also added indexes on "date", "(date, emp_code)", and "(emp_code, punch_in)" to support listing, sorting, and punch-out lookups efficiently. I considered a broad compound index on every summary field, but rejected it because it would be too wide and less useful than the narrower indexes above.

2. !!Punch-in race.!! When two equivalent punch-in requests arrive at the same time, both call "insert_one" against the same collection. MongoDB enforces the unique "(emp_code, date)" index, so only one insert succeeds. The losing request is caught as "DuplicateKeyError" and converted to HTTP 409, while the winner gets 201.

3. !!Ties.!! The leaderboard uses competition ranking: employees with the same total late minutes share a rank, and the next rank is skipped. If the cutoff is "limit", I return every row whose rank is "<= limit", even if that means more than "limit" rows when a tie falls on the cutoff.

4. !!Headcount.!! For the department summary, I count all employees whose "joined_on" is at or before the month-end, regardless of whether they have logs. This is done in the department aggregation before the attendance rows are processed, so zero-log employees still contribute to headcount.

5. !!One thing I would change!! if this had to serve 100x the data is to push more of the summary and trend logic into MongoDB aggregations and add a small caching layer for the read-heavy monthly reports. That keeps the working set in the database and avoids repeating expensive scanning in Python.
