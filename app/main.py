"""HROne Employee Attendance & Analytics API (contract v2.0)."""
from __future__ import annotations

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Existing process environment values take precedence over .env.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MAX_EPOCH_MS = 4_102_444_800_000

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ----------------------------- Validation models -----------------------------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_epoch(cls, v):
        if v is None: return v
        return validate_epoch(v)

    @field_validator("status")
    @classmethod
    def valid_presence(cls, v):
        if v not in PRESENCE: raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_epoch(cls, v):
        if v is None: return v
        return validate_epoch(v)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_optional_epoch(cls, v):
        if v is None: return v
        return validate_epoch(v)

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v is not None and v not in STATUSES: raise ValueError("invalid status")
        return v


def validate_epoch(value: Any) -> int:
    # bool is an int subclass in Python, but is not a valid timestamp.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be an integer epoch-millisecond value")
    if value < 100_000_000_000 or value > MAX_EPOCH_MS:
        raise ValueError("timestamp is outside the accepted epoch-millisecond range")
    return value


def truncate_ms(ms: int) -> int:
    return (ms // 1000) * 1000


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(truncate_ms(ms) / 1000, tz=UTC)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None: return None
    if value.tzinfo is None: return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def round_half_up(value: float, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def safe_doc(doc: Optional[dict]) -> Optional[dict]:
    if doc is None: return None
    result = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("created_at", "punch_in", "punch_out"):
        if field in result: result[field] = to_ms(result[field])
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result["history"] = result.get("history") or []
    result["half_day"] = result.get("half_day", False)
    for entry in result["history"]:
        if "at" in entry: entry["at"] = to_ms(entry["at"])
        for change in (entry.get("changes") or {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime): change[key] = to_ms(change[key])
    return result


def safe_employee(doc: Optional[dict]) -> Optional[dict]:
    if doc is None: return None
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result: result["created_at"] = to_ms(result["created_at"])
    return result


def ensure_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_emp")


@app.on_event("startup")
def startup():
    # Do not block import/readiness startup on a transient database outage; /health reports it.
    try: ensure_indexes()
    except PyMongoError: pass


# ----------------------------- Time-rule helpers -----------------------------
def shift_window(emp: dict, attendance_day: date) -> tuple[datetime, datetime]:
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    start_t, end_t = time(sh, sm), time(eh, em)
    start_day = attendance_day
    start = datetime.combine(start_day, start_t, tzinfo=IST)
    end_day = start_day + (timedelta(days=1) if end_t <= start_t else timedelta())
    end = datetime.combine(end_day, end_t, tzinfo=IST)
    return start.astimezone(UTC), end.astimezone(UTC)


def attendance_day_for(emp: dict, punch_in: datetime) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    start_t = datetime.strptime(emp["shift_start"], "%H:%M").time()
    end_t = datetime.strptime(emp["shift_end"], "%H:%M").time()
    if end_t <= start_t and local.time().replace(tzinfo=None) < end_t:
        day -= timedelta(days=1)
    return day


def calculate_late(emp: dict, day: date, punch_in: datetime) -> int:
    start, _ = shift_window(emp, day)
    elapsed = int((punch_in - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def calculate_derived(emp: dict, rec: dict) -> dict:
    status = rec.get("status", "PRESENT")
    pin, pout = as_utc(rec.get("punch_in")), as_utc(rec.get("punch_out"))
    out = dict(rec)
    out["history"] = rec.get("history") or []
    if status in ("ABSENT", "LEAVE"):
        out.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
        return out
    if pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    day = date.fromisoformat(rec["date"])
    out["punch_in"] = pin
    out["late_minutes"] = calculate_late(emp, day, pin)
    if pout is None:
        out.update(work_hours=None, overtime_minutes=0, half_day=False, punch_out=None)
        return out
    if pout <= pin or pout - pin > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    _, shift_end = shift_window(emp, day)
    seconds = int((pout - pin).total_seconds())
    work_hours = round_half_up(seconds / 3600, 2)
    overtime_seconds = int((pout - shift_end).total_seconds())
    overtime = overtime_seconds // 60 if overtime_seconds >= 1800 else 0
    out.update(punch_out=pout, work_hours=work_hours, overtime_minutes=int(overtime), half_day=work_hours < 4.50)
    return out


def month_dates(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def date_query(date_from: Optional[date], date_to: Optional[date]) -> dict:
    q = {}
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from.isoformat()
        if date_to: q["date"]["$lte"] = date_to.isoformat()
    return q


def explain_aggregate(collection: str, pipeline: list) -> dict:
    return db.command({"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})


# -------------------------------- System / CRUD -------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except PyMongoError:
        raise HTTPException(503, "database operation failed")
    return safe_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    filt = {"department": department} if department is not None else {}
    total = employees.count_documents(filt)
    docs = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [safe_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    instant = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_day_for(emp, instant).isoformat()
    rec = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": instant,
           "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
           "half_day": False, "history": []}
    rec = calculate_derived(emp, rec)
    try:
        attendance.insert_one(rec)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance record already exists for date")
    except PyMongoError:
        raise HTTPException(503, "database operation failed")
    return safe_doc(rec)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in STATUSES: raise HTTPException(422, "invalid status")
    filt = date_query(date_from, date_to)
    if emp_code is not None: filt["emp_code"] = emp_code
    if status is not None: filt["status"] = status
    total = attendance.count_documents(filt)
    cursor = attendance.find(filt).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [safe_doc(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    pout = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    # Find the latest open punch-in not later than punch-out; supports overnight shifts.
    rec = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": pout}}, sort=[("punch_in", DESCENDING)])
    if rec is None: raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    if pout <= as_utc(rec.get("punch_in")) or pout - as_utc(rec["punch_in"]) > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    candidate = calculate_derived(emp, {**rec, "punch_out": pout})
    fields = {k: candidate[k] for k in ("punch_out", "work_hours", "overtime_minutes", "half_day", "late_minutes")}
    result = attendance.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": fields})
    if result.modified_count != 1: raise HTTPException(409, "record already punched out")
    rec.update(fields)
    return safe_doc(rec)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str = Path(min_length=1), date: date = Path(), body: RegularizeRequest = ...):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    key = {"emp_code": emp_code, "date": date.isoformat()}
    rec = attendance.find_one(key)
    if not rec: raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set - {"reason", "regularized_by"}
    if not supplied: raise HTTPException(422, "request must change at least one field")
    proposed = dict(rec)
    for field in supplied:
        value = getattr(body, field)
        if field in ("punch_in", "punch_out") and value is not None: value = from_ms(value)
        proposed[field] = value
    if proposed.get("status") in ("ABSENT", "LEAVE"):
        if ("punch_in" in supplied and body.punch_in is not None) or ("punch_out" in supplied and body.punch_out is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        proposed["punch_in"] = proposed["punch_out"] = None
    if proposed.get("status") in PRESENCE and proposed.get("punch_in") is None:
        raise HTTPException(422, "presence status requires punch_in")
    pin = as_utc(proposed.get("punch_in"))
    if pin is not None and attendance_day_for(emp, pin).isoformat() != date.isoformat():
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    proposed = calculate_derived(emp, proposed)
    changed = {}
    set_fields = {}
    # History records every field that actually changed, including recomputed derived fields.
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old, new = rec.get(field), proposed.get(field)
        if field == "half_day": old, new = bool(old or False), bool(new or False)
        if field in ("punch_in", "punch_out"):
            old, new = as_utc(old), as_utc(new)
        if old != new:
            changed[field] = {"from": old, "to": new}
            set_fields[field] = new
    if not changed: raise HTTPException(422, "request does not change the record")
    at = datetime.now(UTC).replace(microsecond=0)
    entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changed}
    # Pipeline update appends history atomically. Compare original values to detect concurrent edits.
    compare = {"_id": rec["_id"], "status": rec.get("status"), "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    update_stage = {**set_fields, "history": {"$concatArrays": [{"$ifNull": ["$history", []]}, [{"$literal": entry}]]}}
    result = attendance.update_one(compare, [{"$set": update_stage}])
    if result.matched_count != 1: raise HTTPException(409, "record changed concurrently; retry correction")
    return safe_doc(attendance.find_one({"_id": rec["_id"]}))


# ---------------------------------- Analytics ---------------------------------
def month_match(month: str) -> dict:
    start, end = month_dates(month)
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end = month_dates(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "employee not found")
    joined = date.fromisoformat(emp["joined_on"])
    effective_start = max(start, joined)
    working_days = 0 if effective_start > end else sum(1 for i in range((end-effective_start).days+1) if (effective_start + timedelta(days=i)).weekday() < 5)
    pipeline = [
        {"$match": {"emp_code": emp_code, **month_match(month)}},
        {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}, "_half": {"$ifNull": ["$half_day", False]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": "$_day"}, [2,3,4,5,6]]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    row = next(iter(attendance.aggregate(pipeline)), {})
    present = round_half_up(row.get("present_days", 0), 2)
    pct = round_half_up(present / working_days * 100, 4) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0),
            "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start, end = month_dates(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_has_log": {"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, "_half": {"$ifNull": ["$logs.half_day", False]}}},
        {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, [2,3,4,5,6]]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": ["$_has_log", {"$ifNull": ["$logs.late_minutes", 0]}, 0]}},
            "leave_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$eq": ["$logs.status", "LEAVE"]}]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$and": ["$_has_log", {"$eq": ["$logs.status", "ON_DUTY"]}]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
            "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 100]}, 0.5]}]}, 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    return {"month": month, "items": list(employees.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    pipeline = [
        {"$match": month_match(month)},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                     "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return {"month": month, "items": list(attendance.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"), to: date = Query(...)):
    start, end = from_date, to
    if end < start: raise HTTPException(422, "to must be on or after from")
    if (end - start).days > 91: raise HTTPException(422, "date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    # Build every date inside MongoDB, then look up eligible employee attendance for each date.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department", "staff": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"days": {"$map": {
            "input": {"$range": [0, (end-start).days + 1]},
            "as": "offset",
            "in": {"$dateAdd": {
                "startDate": {"$dateFromString": {"dateString": start.isoformat()}},
                "unit": "day", "amount": "$$offset"
            }}
        }}}},
        {"$unwind": "$days"},
        {"$set": {"_eligible": {"$filter": {"input": "$staff", "as": "person", "cond": {"$lte": ["$$person.joined_on", {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}}]}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}}, "codes": {"$map": {"input": "$_eligible", "as": "p", "in": "$$p.emp_code"}}},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}}], "as": "logs"}},
        {"$set": {"is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$days"}, [1, 7]]}]},
            "headcount": {"$size": "$_eligible"},
            "present_count": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [{"$in": ["$$log.status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]}, 0]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}]}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}}, "is_working_day": 1, "headcount": 1, "present_count": {"$round": ["$present_count", 2]}, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$divide": [{"$floor": [{"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}]}, 10000]}]}}},
        {"$sort": {"date": 1}}
    ]
    return {"department": department, "items": list(employees.aggregate(pipeline, allowDiskUse=True))}


# ----------------------------------- Explain ----------------------------------
def attendance_list_pipeline(emp_code, date_from, date_to, status, page, page_size):
    filt = date_query(date_from, date_to)
    if emp_code is not None: filt["emp_code"] = emp_code
    if status is not None: filt["status"] = status
    return [{"$match": filt}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page-1)*page_size}, {"$limit": page_size}]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                    department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                    date_from: Optional[date] = None, date_to: Optional[date] = None, status: Optional[str] = None,
                    from_date: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "invalid endpoint")
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        if status is not None and status not in STATUSES: raise HTTPException(422, "invalid status")
        pipeline = attendance_list_pipeline(emp_code, date_from, date_to, status, page, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        month_dates(month)
        pipeline = [{"$match": {"emp_code": emp_code, **month_match(month)}}, {"$group": {"_id": "$emp_code", "late": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = month_dates(month)
        match = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None: match["department"] = department
        pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}}]
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", pipeline)}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        month_dates(month)
        pipeline = [{"$match": month_match(month)}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}, {"$match": {"total_late_minutes": {"$gt": 0}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$limit": limit}]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}
    if not department or not from_date or not to: raise HTTPException(422, "department, from, and to are required")
    if to < from_date or (to-from_date).days > 91: raise HTTPException(422, "invalid trend date range")
    pipeline = [{"$match": {"department": department}}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}]
    return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", pipeline)}
