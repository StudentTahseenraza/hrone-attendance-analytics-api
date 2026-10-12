"""Tests: pure business rules + CRUD/attendance flows on mongomock.
Aggregation pipelines using $setWindowFields/$densify need a real MongoDB (see README)."""
import os, sys
os.environ.setdefault("MONGO_URI", "mongodb://localhost:1")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from datetime import datetime, timezone
import mongomock, pytest
from fastapi.testclient import TestClient
from app import main as m

IST = m.IST
def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)
def ms(dt): return m.to_ms(dt)

@pytest.fixture()
def c():
    m.db = mongomock.MongoClient(tz_aware=True)["t"]
    m._indexes_ready = False
    m.ensure_indexes()
    return TestClient(m.app)

def emp(c, code="EMP0001", **kw):
    b = dict(emp_code=code, name="A", email="a@x.com", department="Eng", joined_on="2026-01-05"); b.update(kw)
    return c.post("/employees", json=b)

# ---- rules ----
@pytest.mark.parametrize("h,mi,s,exp", [(9,30,0,0),(9,40,0,0),(9,40,1,10),(9,41,0,11),(10,15,59,45),(9,0,0,0)])
def test_late(h, mi, s, exp):
    assert m.compute_late_minutes(ist(2026,7,6,h,mi,s), "2026-07-06", "09:30", "18:30") == exp

@pytest.mark.parametrize("after_secs,exp", [(300,0),(1799,0),(1800,30),(2445,40)])
def test_overtime(after_secs, exp):
    from datetime import timedelta
    assert m.compute_overtime(ist(2026,7,6,18,30)+timedelta(seconds=after_secs), "2026-07-06", "09:30", "18:30") == exp

def test_overnight():
    pin = ist(2026,7,6,21,55)
    assert m.attendance_date(pin, "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026,7,7,5,59,59), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026,7,7,6,0,0), "22:00", "06:00") == "2026-07-07"
    assert m.compute_late_minutes(ist(2026,7,6,22,11), "2026-07-06", "22:00", "06:00") == 11
    assert m.compute_overtime(ist(2026,7,7,6,40), "2026-07-06", "22:00", "06:00") == 40

def test_hours_half_up():
    a = ist(2026,7,6,9,0)
    assert m.compute_work_hours(a, ist(2026,7,6,18,7)) == 9.12
    assert m.compute_work_hours(a, datetime.fromtimestamp(a.timestamp()+3*3600+54, tz=timezone.utc)) == 3.02  # 3.015 -> half-up
    assert m.rnd(2.675, 2) == 2.68 and m.rnd(0.12345, 4) == 0.1235

def test_working_days():
    from datetime import date
    assert m.working_days_between(date(2026,7,1), date(2026,7,31)) == 23
    assert m.working_days_between(date(2026,7,13), date(2026,7,31)) == 15

# ---- API ----
def test_health_and_employees(c, monkeypatch):
    assert emp(c).status_code == 201
    assert emp(c).status_code == 409
    assert emp(c, "EMP0002", shift_start="09:00", shift_end="09:00").status_code == 422
    assert emp(c, "BAD").status_code == 422
    for i in range(3, 8): emp(c, f"EMP000{i}", department="Ops")
    r = c.get("/employees", params={"department": "Ops", "page": 1, "page_size": 2}).json()
    assert r["total"] == 5 and [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"page_size": 101}).status_code == 422
    assert isinstance(r["items"][0]["created_at"], int)

def test_punch_flow(c):
    emp(c)
    pin = ms(ist(2026,7,6,10,5))
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    assert r.status_code == 201, r.text
    j = r.json(); assert j["date"] == "2026-07-06" and j["late_minutes"] == 35 and j["punch_in"] == pin and "id" not in j
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin}).status_code == 409
    assert c.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    for bad in (1783312500, 1.5e12, "1783312500000", True):
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    out = ms(ist(2026,7,6,19,10))
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).json()
    assert r["work_hours"] == 9.08 and r["overtime_minutes"] == 40 and r["half_day"] is False and r["history"] == []
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).status_code == 409
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": out}).status_code == 404

def test_punch_out_rules(c):
    emp(c, shift_start="22:00", shift_end="06:00")
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026,7,6,21,55))})
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026,7,6,21,55))}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026,7,7,22,0))}).status_code == 422
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026,7,7,6,40))}).json()
    assert r["date"] == "2026-07-06" and r["work_hours"] == 8.75 and r["overtime_minutes"] == 40

def test_list_and_regularize(c):
    emp(c)
    for d in (6, 7, 8):
        c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026,7,d,10,5))})
    r = c.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07"}).json()
    assert r["total"] == 2 and [i["date"] for i in r["items"]] == ["2026-07-07", "2026-07-06"]
    assert c.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-07"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "2026-13-01"}).status_code == 422
    new_in = ms(ist(2026,7,7,9,28))
    body = {"regularized_by": "hr", "reason": "biometric glitch", "punch_in": new_in}
    r = c.patch("/attendance/EMP0001/2026-07-07", json=body)
    assert r.status_code == 200, r.text
    j = r.json(); h = j["history"]
    assert len(h) == 1 and j["late_minutes"] == 0 and h[0]["changes"]["punch_in"]["to"] == new_in
    assert set(h[0]["changes"]) == {"punch_in", "late_minutes"}
    assert c.patch("/attendance/EMP0001/2026-07-07", json=body).status_code == 422  # nothing changes
    r = c.patch("/attendance/EMP0001/2026-07-07", json={"regularized_by": "hr", "reason": "marked leave", "status": "LEAVE"}).json()
    assert r["punch_in"] is None and len(r["history"]) == 2
    assert c.patch("/attendance/EMP0001/2026-07-07", json={"regularized_by": "hr", "reason": "marked leave", "status": "LEAVE", "punch_in": new_in}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-07", json={"regularized_by": "hr", "reason": "back again", "status": "PRESENT"}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-20", json=body).status_code == 404
    assert c.patch("/attendance/NOPE/2026-07-07", json=body).status_code == 404
    assert c.patch("/attendance/EMP0001/bad", json=body).status_code == 422
