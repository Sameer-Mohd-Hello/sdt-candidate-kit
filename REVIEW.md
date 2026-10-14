# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | Pagination used `skip = page * page_size` instead of `(page - 1) * page_size`, so page 2 and later returned duplicates or skipped rows. | Request `/employees?page=2&page_size=20`; the second page is wrong or overlaps. | Switched to the correct offset and kept filtering in the query. |
| 2 | `punch_in` | Duplicate attendance records were checked with a read-then-insert pattern; two simultaneous punches could both pass and create the same `(emp_code, date)` document. | Two identical requests at the same instant can create duplicate attendance rows. | Added a unique index on `(emp_code, date)` and caught `DuplicateKeyError` to return 409. |
| 3 | `compute_late_minutes`, `compute_overtime`, and the starter date logic | Late and overtime were computed with naive local-time logic and ignored overnight shifts and the 10-minute grace rule. | A `22:00-06:00` shift or a late punch like 09:40:01 shows the wrong minutes. | Replaced the calculations with IST-aware logic that respects shift dates, overnight shifts, and half-up rounding. |
| 4 | `/health` | The health endpoint did not check MongoDB at all and always returned 200, which makes readiness checks lie. | A MongoDB outage still returns `status: ok`. | Added a real `ping` check and return 503 when MongoDB is unavailable. |
| 5 | `list_attendance` | The route loaded the entire collection into Python and sorted in memory, which does not scale to a ~100k-row dataset. | A larger dataset makes listing attendance noticeably slower or can hit memory pressure. | Kept the query in MongoDB with a sort and pagination on the indexed fields. |
| 6 | `punch_out` and the missing contract endpoints | The starter never implemented the punch-out or analytics endpoints, so the API contract was incomplete. | Calls to `/attendance/punch-out`, `/analytics/...`, or `/admin/explain/...` return 404 or fail. | Implemented the punch-out flow, analytics summaries, and admin explain support to match the OpenAPI contract. |

Also note anything you looked at and decided was not a defect, and why.

- I did not mark the `created_at` field as a defect: it is meant to be server-created and is not client-supplied by design.
- I did not mark the presence-status constants as a defect: `PRESENT`, `WFH`, and `ON_DUTY` are intentionally restricted to the punch-in endpoint and are valid per the contract.
- I did not treat the use of MongoDB's `_id` as a defect: the API correctly uses `emp_code` and `(emp_code, date)` as the natural keys while keeping `_id` internal.
