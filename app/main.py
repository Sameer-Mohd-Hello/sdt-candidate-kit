# uvicorn app.main:app --port 8000
import os
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUS_VALUES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def to_utc_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return value


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def round_half_up(value: float, digits: int) -> float:
    quant = Decimal("1") if digits == 0 else Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def parse_hhmm(value: str) -> time:
    return time.fromisoformat(value)


def iso_date(value: str) -> date:
    return date.fromisoformat(value)


def shift_start_dt_for_record(record_date: str, shift_start: str) -> datetime:
    return datetime.combine(iso_date(record_date), parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt_for_record(record_date: str, shift_end: str, shift_start: str) -> datetime:
    end_dt = datetime.combine(iso_date(record_date), parse_hhmm(shift_end), tzinfo=IST)
    if shift_end <= shift_start:
        return end_dt + timedelta(days=1)
    return end_dt


def calc_attendance_date_for_punch_in(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_local = punch_in.astimezone(IST)
    if shift_end <= shift_start:
        end_local = datetime.combine(punch_local.date(), parse_hhmm(shift_end), tzinfo=IST)
        if punch_local.time() < end_local.time():
            return (punch_local.date() - timedelta(days=1)).isoformat()
    return punch_local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str) -> int:
    start_dt = shift_start_dt_for_record(record_date, shift_start)
    delta_minutes = (punch_in.astimezone(IST) - start_dt).total_seconds() / 60
    if delta_minutes <= 10:
        return 0
    return int(delta_minutes)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, shift_start: str, record_date: str) -> int:
    end_dt = shift_end_dt_for_record(record_date, shift_end, shift_start)
    delta_minutes = (punch_out.astimezone(IST) - end_dt).total_seconds() / 60
    if delta_minutes < 30:
        return 0
    return int(delta_minutes)


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.5


def serialize_history(history: list[dict]) -> list[dict]:
    out: list[dict] = []
    for entry in history or []:
        changes = {}
        for field, value in (entry.get("changes") or {}).items():
            if not isinstance(value, dict):
                changes[field] = value
                continue
            changes[field] = {
                "from": to_utc_ms(value.get("from")),
                "to": to_utc_ms(value.get("to")),
            }
        out.append({
            "at": to_utc_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return out


def serialize_attendance_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_utc_ms(doc.get("punch_in")),
        "punch_out": to_utc_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history") or []),
    }


def serialize_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_utc_ms(out.get("created_at"))
    return out


def ensure_employee_exists(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


def ensure_attendance_record(emp_code: str, record_date: str) -> dict:
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if not doc:
        raise HTTPException(status_code=404, detail="attendance record not found")
    return doc


def validate_punch_in_time(punch_in: datetime, record_date: str, shift_start: str, shift_end: str) -> None:
    if calc_attendance_date_for_punch_in(punch_in, shift_start, shift_end) != record_date:
        raise HTTPException(status_code=422, detail="punch_in must stay on the record date")


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])


@app.on_event("startup")
def startup_event() -> None:
    ensure_indexes()


@app.get("/health")
def health() -> dict:
    try:
        db.command("ping")
    except Exception as exc:  # pragma: no cover - environmental
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT")


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    existing = db.employees.find_one({"emp_code": body.emp_code})
    if existing:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = []
    for doc in db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size):
        items.append(serialize_employee(doc))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = ensure_employee_exists(body.emp_code)
    if body.status not in PRESENT_STATUSES:
        raise HTTPException(status_code=422, detail="status must be PRESENT, WFH, or ON_DUTY")

    ts = from_epoch_ms(body.punched_at) or datetime.now(timezone.utc)
    record_date = calc_attendance_date_for_punch_in(ts, emp["shift_start"], emp["shift_end"])
    try:
        doc = {
            "emp_code": body.emp_code,
            "date": record_date,
            "status": body.status,
            "punch_in": ts,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": compute_late_minutes(ts, emp["shift_start"], record_date),
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    ensure_employee_exists(body.emp_code)
    ts = from_epoch_ms(body.punched_at) or datetime.now(timezone.utc)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if not doc:
        raise HTTPException(status_code=404, detail="no punch-in found")

    emp = ensure_employee_exists(body.emp_code)
    if doc["punch_in"] is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if ts <= doc["punch_in"]:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if (ts - doc["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    punch_in_dt = doc["punch_in"]
    punch_out_dt = ts
    employee = db.employees.find_one({"emp_code": body.emp_code})
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime_minutes(punch_out_dt, employee["shift_end"], employee["shift_start"], doc["date"])
    half_day = compute_half_day(work_hours)
    update = {
        "punch_out": punch_out_dt,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    db.attendance_logs.update_one({"_id": doc["_id"]}, {"$set": update})
    doc.update(update)
    return serialize_attendance_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    items = []
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    for doc in cursor:
        items.append(serialize_attendance_record(doc))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = ensure_employee_exists(emp_code)
    doc = ensure_attendance_record(emp_code, date)
    old_doc = {k: v for k, v in doc.items() if k != "_id"}
    updated = dict(old_doc)
    changes: dict[str, dict[str, Any]] = {}

    if body.status is not None:
        if body.status not in ALL_STATUS_VALUES:
            raise HTTPException(status_code=422, detail="invalid status")
        if body.status in {"ABSENT", "LEAVE"}:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot specify punch times")
            if updated.get("status") != body.status:
                changes["status"] = {"from": updated.get("status"), "to": body.status}
            updated["status"] = body.status
            updated["punch_in"] = None
            updated["punch_out"] = None
            updated["work_hours"] = None
            updated["late_minutes"] = 0
            updated["overtime_minutes"] = 0
            updated["half_day"] = False
        else:
            if updated.get("status") != body.status:
                changes["status"] = {"from": updated.get("status"), "to": body.status}
            updated["status"] = body.status

    if body.punch_in is not None:
        punch_in_dt = from_epoch_ms(body.punch_in)
        if punch_in_dt is None:
            raise HTTPException(status_code=422, detail="invalid punch_in")
        if updated.get("status") in {"ABSENT", "LEAVE"}:
            raise HTTPException(status_code=422, detail="punch_in cannot be set for ABSENT/LEAVE")
        validate_punch_in_time(punch_in_dt, date, emp["shift_start"], emp["shift_end"])
        if updated.get("punch_in") != punch_in_dt:
            changes["punch_in"] = {"from": to_utc_ms(updated.get("punch_in")), "to": body.punch_in}
        updated["punch_in"] = punch_in_dt

    if body.punch_out is not None:
        punch_out_dt = from_epoch_ms(body.punch_out)
        if punch_out_dt is None:
            raise HTTPException(status_code=422, detail="invalid punch_out")
        if updated.get("status") in {"ABSENT", "LEAVE"}:
            raise HTTPException(status_code=422, detail="punch_out cannot be set for ABSENT/LEAVE")
        if updated.get("punch_in") is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if punch_out_dt <= updated["punch_in"]:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (punch_out_dt - updated["punch_in"]).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        if updated.get("punch_out") != punch_out_dt:
            changes["punch_out"] = {"from": to_utc_ms(updated.get("punch_out")), "to": body.punch_out}
        updated["punch_out"] = punch_out_dt

    if updated.get("status") in {"ABSENT", "LEAVE"}:
        pass
    elif updated.get("status") in PRESENT_STATUSES:
        if updated.get("punch_in") is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if body.punch_out is None and updated.get("punch_out") is not None:
            pass

    if updated.get("punch_in") is not None and updated.get("punch_out") is not None:
        work_hours = compute_work_hours(updated["punch_in"], updated["punch_out"])
        late = compute_late_minutes(updated["punch_in"], emp["shift_start"], date)
        overtime = compute_overtime_minutes(updated["punch_out"], emp["shift_end"], emp["shift_start"], date)
        half_day = compute_half_day(work_hours)
        if updated.get("work_hours") != work_hours:
            changes["work_hours"] = {"from": updated.get("work_hours"), "to": work_hours}
        if updated.get("late_minutes") != late:
            changes["late_minutes"] = {"from": updated.get("late_minutes"), "to": late}
        if updated.get("overtime_minutes") != overtime:
            changes["overtime_minutes"] = {"from": updated.get("overtime_minutes"), "to": overtime}
        if updated.get("half_day") != half_day:
            changes["half_day"] = {"from": updated.get("half_day"), "to": half_day}
        updated["work_hours"] = work_hours
        updated["late_minutes"] = late
        updated["overtime_minutes"] = overtime
        updated["half_day"] = half_day
    elif updated.get("status") in PRESENT_STATUSES and updated.get("punch_in") is not None:
        late = compute_late_minutes(updated["punch_in"], emp["shift_start"], date)
        if updated.get("late_minutes") != late:
            changes["late_minutes"] = {"from": updated.get("late_minutes"), "to": late}
        updated["late_minutes"] = late
        updated["work_hours"] = None
        updated["overtime_minutes"] = 0
        updated["half_day"] = False
    else:
        updated["late_minutes"] = 0
        updated["overtime_minutes"] = 0
        updated["half_day"] = False
        updated["work_hours"] = None

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated["history"] = list(doc.get("history") or []) + [history_entry]
    db.attendance_logs.update_one({"_id": doc["_id"]}, {"$set": {
        "status": updated["status"],
        "punch_in": updated.get("punch_in"),
        "punch_out": updated.get("punch_out"),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes"),
        "overtime_minutes": updated.get("overtime_minutes"),
        "half_day": updated.get("half_day"),
        "history": updated["history"],
    }})
    return serialize_attendance_record(updated)


def month_range_for(month: str):
    year, month_num = month.split("-")
    year_i = int(year)
    month_i = int(month_num)
    last_day = monthrange(year_i, month_i)[1]
    return date(year_i, month_i, 1), date(year_i, month_i, last_day)


def round_rate(value: float, digits: int = 4) -> float:
    return round_half_up(value, digits)


def working_days_count(joined_on: str, month: str) -> int:
    month_start, month_end = month_range_for(month)
    joined = iso_date(joined_on)
    if joined > month_end:
        return 0
    start_day = max(month_start, joined)
    days = 0
    curr = start_day
    while curr <= month_end:
        if curr.weekday() < 5:
            days += 1
        curr += timedelta(days=1)
    return days


def present_day_value(record: dict) -> float:
    if record.get("status") not in PRESENT_STATUSES:
        return 0.0
    if record.get("half_day"):
        return 0.5
    return 1.0


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = ensure_employee_exists(emp_code)
    month_start, month_end = month_range_for(month)
    docs = list(db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()},
    }))

    working_days = working_days_count(emp["joined_on"], month)
    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for doc in docs:
        rec_date = iso_date(doc["date"])
        if rec_date.weekday() < 5 and rec_date >= iso_date(emp["joined_on"]):
            present_days += present_day_value(doc)
        if doc.get("status") == "LEAVE":
            leave_days += 1
        if doc.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(doc.get("late_minutes", 0) or 0)
        total_overtime_minutes += int(doc.get("overtime_minutes", 0) or 0)

    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_start, month_end = month_range_for(month)
    employees = list(db.employees.find({"joined_on": {"$lte": month_end.isoformat()}}))
    if department is not None:
        employees = [e for e in employees if e["department"] == department]
    by_emp = {emp["emp_code"]: emp for emp in employees}
    dept_headcount = {}
    for emp in employees:
        dept_headcount.setdefault(emp["department"], 0)
        dept_headcount[emp["department"]] += 1

    attendance = list(db.attendance_logs.find({
        "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()},
    }))
    metrics = {}
    for dept in dept_headcount:
        metrics[dept] = {
            "department": dept,
            "headcount": dept_headcount[dept],
            "present_days": 0.0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
            "work_hours_values": [],
        }

    for doc in attendance:
        emp = by_emp.get(doc["emp_code"])
        if not emp:
            continue
        dept = emp["department"]
        record = metrics.setdefault(dept, {
            "department": dept,
            "headcount": dept_headcount.get(dept, 0),
            "present_days": 0.0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
            "work_hours_values": [],
        })
        rec_dt = iso_date(doc["date"])
        if rec_dt.weekday() < 5 and doc.get("status") in PRESENT_STATUSES:
            record["present_days"] += 0.5 if doc.get("half_day") else 1.0
        if doc.get("late_minutes", 0) > 0:
            record["late_count"] += 1
            record["total_late_minutes"] += int(doc.get("late_minutes", 0) or 0)
        if doc.get("status") == "LEAVE":
            record["leave_count"] += 1
        if doc.get("status") == "ON_DUTY":
            record["on_duty_count"] += 1
        if doc.get("status") in PRESENT_STATUSES and doc.get("work_hours") is not None:
            record["work_hours_values"].append(float(doc["work_hours"]))

    items = []
    for dept, row in sorted(metrics.items()):
        if row["headcount"] == 0:
            continue
        avg = None
        if row["work_hours_values"]:
            avg = round_half_up(sum(row["work_hours_values"]) / len(row["work_hours_values"]), 2)
        items.append({
            "department": dept,
            "headcount": row["headcount"],
            "present_days": float(row["present_days"]),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(default=10, ge=1, le=50), department: Optional[str] = None):
    month_start, month_end = month_range_for(month)
    q = {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}
    if department is not None:
        employee_ids = [emp["emp_code"] for emp in db.employees.find({"department": department})]
        if not employee_ids:
            return {"month": month, "items": []}
        q["emp_code"] = {"$in": employee_ids}

    rows = {}
    for doc in db.attendance_logs.find(q):
        late = int(doc.get("late_minutes", 0) or 0)
        if late <= 0:
            continue
        emp = db.employees.find_one({"emp_code": doc["emp_code"]})
        if not emp:
            continue
        if department is not None and emp["department"] != department:
            continue
        entry = rows.setdefault(doc["emp_code"], {"emp_code": doc["emp_code"], "name": emp["name"], "department": emp["department"], "total_late_minutes": 0, "late_count": 0})
        entry["total_late_minutes"] += late
        entry["late_count"] += 1

    ordered = sorted(rows.values(), key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
    ranked = []
    current_rank = 0
    previous_total = None
    for index, item in enumerate(ordered, start=1):
        if previous_total is None or item["total_late_minutes"] != previous_total:
            current_rank = index
            previous_total = item["total_late_minutes"]
        ranked.append({
            "rank": current_rank,
            "emp_code": item["emp_code"],
            "name": item["name"],
            "department": item["department"],
            "total_late_minutes": item["total_late_minutes"],
            "late_count": item["late_count"],
        })

    selected = [item for item in ranked if item["rank"] <= limit]
    return {"month": month, "items": selected}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):
    start = iso_date(from_)
    end = iso_date(to)
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")
    employees = list(db.employees.find({"department": department}))
    if not employees:
        raise HTTPException(status_code=404, detail="department not found")

    daily_rows = []
    current = start
    while current <= end:
        daily_rows.append(current)
        current += timedelta(days=1)

    emp_lookup = {emp["emp_code"]: emp for emp in employees}
    logs_by_date = {}
    for log_doc in db.attendance_logs.find({"emp_code": {"$in": list(emp_lookup.keys())}, "date": {"$gte": from_, "$lte": to}}):
        logs_by_date.setdefault(log_doc["date"], []).append(log_doc)

    results = []
    rates = []
    for current_day in daily_rows:
        date_str = current_day.isoformat()
        headcount = 0
        for emp in employees:
            if iso_date(emp["joined_on"]) <= current_day:
                headcount += 1
        present_count = 0.0
        late_count = 0
        for log_doc in logs_by_date.get(date_str, []):
            if log_doc.get("status") in PRESENT_STATUSES:
                present_count += 0.5 if log_doc.get("half_day") else 1.0
            if log_doc.get("late_minutes", 0) > 0:
                late_count += 1
        attendance_rate = None
        if current_day.weekday() < 5:
            if headcount > 0:
                attendance_rate = round_half_up(present_count / headcount, 4)
            else:
                attendance_rate = 0.0
        results.append({
            "date": date_str,
            "is_working_day": current_day.weekday() < 5,
            "headcount": headcount,
            "present_count": float(present_count),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
        })
        if attendance_rate is not None:
            rates.append(attendance_rate)

    moving = []
    window = []
    for idx, row in enumerate(results):
        if row["attendance_rate"] is not None:
            window.append(row["attendance_rate"])
            if len(window) > 7:
                window.pop(0)
        if not window:
            moving_value = None
        else:
            recent = window[-7:]
            moving_value = round_half_up(sum(recent) / len(recent), 4)
        row["moving_avg_7d"] = moving_value
        moving.append(row)

    return {"department": department, "items": moving}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        explain = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, end = month_range_for(month)
        query = {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        explain = db.attendance_logs.find(query).sort([("date", -1)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end = month_range_for(month)
        query = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        if department is not None:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department})]
            if not emp_codes:
                return {"endpoint": endpoint, "collection": "attendance_logs", "explain": {"note": "department has no employees"}}
            query["emp_code"] = {"$in": emp_codes}
        explain = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end = month_range_for(month)
        query = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}
        if department is not None:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department})]
            query["emp_code"] = {"$in": emp_codes}
        explain = db.attendance_logs.find(query).sort([("late_minutes", -1), ("emp_code", 1)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        if iso_date(to) < iso_date(from_):
            raise HTTPException(status_code=422, detail="to must be on or after from")
        employees = list(db.employees.find({"department": department}, {"emp_code": 1, "_id": 0}))
        if not employees:
            raise HTTPException(status_code=404, detail="department not found")
        emp_codes = [e["emp_code"] for e in employees]
        query = {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}
        explain = db.attendance_logs.find(query).sort([("date", 1), ("emp_code", 1)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(status_code=422, detail="invalid endpoint")
