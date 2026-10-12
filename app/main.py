"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (real environment variables win over a local .env)

Layout of this single file
  1. configuration / Mongo client / index creation
  2. small helpers (time, rounding, errors, serialisation)
  3. business rules R1-R5 (pure functions)
  4. request models
  5. endpoints: system, employees, attendance
  6. aggregation-pipeline builders + analytics endpoints
  7. explain endpoint
"""
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables already present in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, client, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes instead of naive ones,
# so we never mix naive and aware values.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index we rely on. Idempotent; safe to call repeatedly."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return
        # Uniqueness guarantees (enforced by the database, not by check-then-insert).
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
        )
        # Query-driven indexes.
        # attendance list / leaderboard: date range + (date desc, emp_code asc) sort
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_emp"
        )
        # punch-out: latest record of one employee with punch_in <= t
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_emp_punch_in"
        )
        # department summary / trend: employees by department + joined_on, and by joined_on alone
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_dept_joined"
        )
        db.employees.create_index([("joined_on", ASCENDING)], name="ix_joined")
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # Mongo may not be up yet; /health will retry and report 503
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENT_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")
MAX_SHIFT_SECONDS = 24 * 3600

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# Strict: floats / strings / seconds-looking numbers are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

DATE_RE_MSG = "must be a valid calendar date in YYYY-MM-DD format"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def unprocessable(loc: tuple, msg: str):
    """Raise a 422 with FastAPI's default {"detail": [...]} body."""
    raise RequestValidationError([{"type": "value_error", "loc": list(loc), "msg": msg, "input": None}])


def parse_date(value: str, loc: tuple) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        unprocessable(loc, DATE_RE_MSG)
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, DATE_RE_MSG)


def now_utc() -> datetime:
    """Current instant, whole seconds, UTC-aware."""
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """epoch milliseconds -> UTC-aware datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def trunc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0)


def rnd(value, places: int = 2) -> Optional[float]:
    """Half-up rounding for reported numbers (R8). Works on int/float/Decimal."""
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    d = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    d = d.quantize(Decimal(1).scaleb(-12), rounding=ROUND_HALF_EVEN)  # strip binary-float noise
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_decimal(value) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def serialize_history(history: Optional[list]) -> list:
    out = []
    for h in history or []:
        changes = {}
        for key, ch in (h.get("changes") or {}).items():
            if key in ("punch_in", "punch_out"):
                changes[key] = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
            else:
                changes[key] = {"from": ch.get("from"), "to": ch.get("to")}
        out.append({"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return out


def serialize_attendance(d: dict) -> dict:
    """Stored document -> API representation (no _id, instants as epoch ms, legacy gaps filled)."""
    wh = d.get("work_hours")
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": None if wh is None else float(wh),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day") or False),
        "history": serialize_history(d.get("history")),
    }


def serialize_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 3. Business rules R1-R5 (pure functions, easy to unit test)
# --------------------------------------------------------------------------- #
def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight-shift punch-in before shift_end
    belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.strftime("%H:%M:%S") < shift_end + ":00":
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Real instants of shift start / end for the shift that began on `date_str` (IST)."""
    d = date.fromisoformat(date_str)
    start = datetime.combine(d, _hm(shift_start), tzinfo=IST)
    end = datetime.combine(d, _hm(shift_end), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly > 10:00 after shift start; then floor(minutes since start)."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    delta = int((punch_in - start).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (exact Decimal arithmetic)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes) after shift end, counted only when >= 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    secs = int((punch_out - end).total_seconds())
    minutes = secs // 60 if secs > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_fields(emp: dict, date_str: str, status: str, pin: Optional[datetime], pout: Optional[datetime]) -> dict:
    """All stored derived values for a record (see DATA_MODEL.md table)."""
    if status in NON_PRESENT_STATUSES or pin is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    ss, se = emp["shift_start"], emp["shift_end"]
    late = compute_late_minutes(pin, date_str, ss, se)
    if pout is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(pin, pout)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(pout, date_str, ss, se),
        "half_day": Decimal(repr(wh)) < HALF_DAY_BELOW,  # R5: compare the ROUNDED value
    }


def working_days_between(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). Pure calendar arithmetic, no data involved."""
    if start > end:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if len(v) != 10 or v[4] != "-" or v[7] != "-":
            raise ValueError(DATE_RE_MSG)
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(DATE_RE_MSG)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    try:
        ensure_indexes()  # retry here if Mongo was down at startup
    except Exception:
        log.exception("index creation failed")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created = datetime.now(UTC)
    doc["created_at"] = created.replace(microsecond=(created.microsecond // 1000) * 1000)  # BSON keeps ms
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the real guard
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)  # same filter as the page itself
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record of this employee whose punch_in is at or before the punch-out
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc(rec["punch_in"])
    if ts <= pin:
        unprocessable(("body", "punched_at"), "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > MAX_SHIFT_SECONDS:
        unprocessable(("body", "punched_at"), "punch-out is more than 24 hours after punch-in")
    derived = derive_fields(emp, rec["date"], rec["status"], pin, ts)
    # atomic close: only succeeds while punch_out is still null (a concurrent winner makes this miss)
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


def build_attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    df = parse_date(date_from, ("query", "date_from")) if date_from else None
    dt = parse_date(date_to, ("query", "date_to")) if date_to else None
    if df and dt and df > dt:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status
    return q


ATT_SORT = {"date": DESCENDING, "emp_code": ASCENDING}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(list(ATT_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_pin, old_pout = trunc(rec.get("punch_in")), trunc(rec.get("punch_out"))
    old_hist = rec.get("history") or []
    old = {
        "status": rec["status"],
        "punch_in": old_pin,
        "punch_out": old_pout,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }

    new_status = body.status or old["status"]
    if new_status in NON_PRESENT_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(("body",), f"status {new_status} clears punch times; do not send punch_in/punch_out")
        new_pin = new_pout = None
    else:
        new_pin = from_ms(body.punch_in) if body.punch_in is not None else old_pin
        new_pout = from_ms(body.punch_out) if body.punch_out is not None else old_pout
        if new_pin is None:
            unprocessable(("body", "punch_in"), "a presence status requires punch_in")
        if attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            unprocessable(("body", "punch_in"), "punch_in must fall on the record's attendance date")
        if new_pout is not None:
            if new_pout <= new_pin:
                unprocessable(("body", "punch_out"), "punch_out must be after punch_in")
            if (new_pout - new_pin).total_seconds() > MAX_SHIFT_SECONDS:
                unprocessable(("body", "punch_out"), "punch_out is more than 24 hours after punch_in")

    new = {"status": new_status, "punch_in": new_pin, "punch_out": new_pout}
    new.update(derive_fields(emp, date, new_status, new_pin, new_pout))
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        unprocessable(("body",), "the request changes nothing")

    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: only apply if the record is exactly as we read it (history length and
    # the editable fields). A concurrent edit or punch-out makes this match nothing -> 409.
    guard = {
        "emp_code": emp_code,
        "date": date,
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(old_hist)]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: pipeline builders + endpoints
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions. They read STORED derived fields and tolerate legacy gaps.
E_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
E_WEEKDAY = {
    "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]
}  # $dayOfWeek: 1 = Sunday ... 7 = Saturday
E_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
E_CREDIT = {"$cond": [E_HALF, 0.5, 1]}  # a half day counts as 0.5
E_LATE = {"$ifNull": ["$late_minutes", 0]}
E_OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
E_IS_LATE = {"$gt": [E_LATE, 0]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def month_range(month: str) -> tuple[str, str, date, date]:
    first, last = month_bounds(month)
    return first.isoformat(), last.isoformat(), first, last


# ---- A. employee monthly ---------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last, _, _ = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # uses (emp_code, date)
        {
            "$group": {
                "_id": None,
                # present_days: presence status AND Monday-Friday; half day = 0.5
                "present_days": {"$sum": {"$cond": [{"$and": [E_PRESENT, E_WEEKDAY]}, E_CREDIT, 0]}},
                "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": _count_if(E_IS_LATE),
                "total_late_minutes": {"$sum": E_LATE},  # weekend records included
                "total_overtime_minutes": {"$sum": E_OVERTIME},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    _, _, first, last = month_range(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = working_days_between(start, last)
    row = next(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)), None) or {}
    present = to_decimal(row.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = rnd(present / Decimal(working_days) * 100, 4)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": rnd(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---- B. department summary -------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last, _, _ = month_range(month)
    emp_match: dict = {"joined_on": {"$lte": last}}  # R9 headcount: joined by month end
    if department:
        emp_match["department"] = department
    has_wh = {"$and": [E_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        # Start from EMPLOYEES so people with zero logs still count towards headcount.
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",  # equality on the indexed field ...
                "pipeline": [  # ... then the month filter, then per-employee totals
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": {"$cond": [{"$and": [E_PRESENT, E_WEEKDAY]}, E_CREDIT, 0]}},
                            # exact decimal sum of work_hours over present records that have a value
                            "wh_sum": {"$sum": {"$cond": [has_wh, {"$toDecimal": "$work_hours"}, 0]}},
                            "wh_count": _count_if(has_wh),
                            "late_count": _count_if(E_IS_LATE),
                            "total_late_minutes": {"$sum": {"$cond": [E_IS_LATE, E_LATE, 0]}},
                            "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
                            "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$addFields": {"s": {"$ifNull": [{"$first": "$stats"}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
                "wh_sum": {"$sum": {"$ifNull": ["$s.wh_sum", 0]}},
                "wh_count": {"$sum": {"$ifNull": ["$s.wh_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(build_department_summary_pipeline(month, department)):
        n = int(r["wh_count"])
        avg = None if n == 0 else rnd(to_decimal(r["wh_sum"]) / Decimal(n), 2)  # mean over records
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": rnd(to_decimal(r["present_days"]), 2),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# ---- C. late leaderboard ---------------------------------------------------- #
def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last, _, _ = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},  # uses (date, emp_code)
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank INSIDE the department
    pipeline += [
        # standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# ---- D. department trend ---------------------------------------------------- #
MAX_TREND_DAYS = 92


def validate_trend_range(frm: str, to: str) -> tuple[date, date]:
    f, t = parse_date(frm, ("query", "from")), parse_date(to, ("query", "to"))
    if t < f:
        unprocessable(("query", "to"), "to must not be before from")
    if (t - f).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return f, t


def trend_members(department: str) -> list[dict]:
    return list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))


def build_trend_pipeline(department: str, members: list[dict], f: date, t: date) -> list:
    codes = [m["emp_code"] for m in members]
    joined = sorted(m["joined_on"] for m in members)
    f_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)  # calendar days are modelled as UTC midnights
    t_excl = datetime(t.year, t.month, t.day, tzinfo=UTC) + timedelta(days=1)  # $densify upper bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": f.isoformat(), "$lte": t.isoformat()}}},
        {
            "$project": {
                "_id": 0,
                "d": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "pc": {"$cond": [E_PRESENT, E_CREDIT, 0]},  # any weekday or weekend, half day = 0.5
                "lc": {"$cond": [E_IS_LATE, 1, 0]},
            }
        },
        # one synthetic zero row on day `from` guarantees $densify always has input, even if no logs exist
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$replaceRoot": {"newRoot": {"$literal": {"d": f_dt, "pc": 0, "lc": 0}}}},
                ],
            }
        },
        {"$group": {"_id": "$d", "pc": {"$sum": "$pc"}, "lc": {"$sum": "$lc"}}},
        {"$project": {"_id": 0, "d": "$_id", "pc": 1, "lc": 1}},
        # gap filling: exactly one row per calendar day in [from, to]
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [f_dt, t_excl]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                "pc": {"$ifNull": ["$pc", 0]},
                "lc": {"$ifNull": ["$lc", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            }
        },
        {
            "$addFields": {  # R9: employees of the department who joined on or before that day
                "headcount": {
                    "$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}
                }
            }
        },
        {
            "$addFields": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$pc", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # 7-day moving mean of the non-null rates: current row + up to 6 preceding rows inside the range
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "pc": 1, "lc": 1, "rate": 1, "moving": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    frm: str = Query(..., alias="from"),
    to: str = Query(...),
):
    f, t = validate_trend_range(frm, to)
    members = trend_members(department)
    if not members:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.attendance_logs.aggregate(build_trend_pipeline(department, members, f, t)):
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": rnd(r["pc"], 2),
                "late_count": int(r["lc"]),
                "attendance_rate": rnd(r["rate"], 4),
                "moving_avg_7d": rnd(r.get("moving"), 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(value, name: str):
    if value is None or value == "":
        unprocessable(("query", name), f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs MongoDB `explain` (executionStats) on the SAME query/pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        cmd = {
            "find": coll,
            "filter": q,
            "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            coll = "attendance_logs"
            pipeline = build_employee_monthly_pipeline(_need(emp_code, "emp_code"), _need(month, "month"))
        elif endpoint == "department_summary":
            coll = "employees"
            pipeline = build_department_summary_pipeline(_need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            coll = "attendance_logs"
            pipeline = build_leaderboard_pipeline(_need(month, "month"), limit, department)
        else:  # department_trend
            coll = "attendance_logs"
            dept = _need(department, "department")
            f, t = validate_trend_range(_need(frm, "from"), _need(to, "to"))
            pipeline = build_trend_pipeline(dept, trend_members(dept), f, t)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    raw = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": raw}
