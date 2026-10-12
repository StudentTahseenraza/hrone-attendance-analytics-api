
import os
from datetime import date, datetime, time, timedelta, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING
# from dotenv import load_dotenv
# load_dotenv()

URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("MONGO_DB", "hrone_loadtest")

# Safety guard: do not seed your normal application database.
if not URI:
    raise RuntimeError("Set MONGO_URI before running this script.")
if DB_NAME != "hrone_loadtest":
    raise RuntimeError("Set MONGO_DB=hrone_loadtest before seeding.")

client = MongoClient(URI, serverSelectionTimeoutMS=10000)
client.admin.command("ping")
db = client[DB_NAME]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Refuse to overwrite existing test data.
if db.employees.count_documents({}) or db.attendance_logs.count_documents({}):
    raise RuntimeError(
        "Test database is not empty. Use a fresh database before seeding."
    )

departments = ["Engineering", "HR", "Finance", "Sales", "Operations"]
employee_count = 1000
day_count = 100
start_date = date(2026, 1, 1)

employees = []
for i in range(employee_count):
    employees.append({
        "emp_code": f"EMP{i + 1:05d}",
        "name": f"Test Employee {i + 1}",
        "email": f"employee{i + 1}@example.com",
        "department": departments[i % len(departments)],
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2025-01-01",
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
    })

db.employees.insert_many(employees, ordered=False)

# Same key indexes as the application.
db.employees.create_index(
    [("emp_code", ASCENDING)], unique=True, name="uq_emp_code"
)
db.employees.create_index(
    [("department", ASCENDING), ("joined_on", ASCENDING)],
    name="ix_dept_joined",
)
db.employees.create_index([("joined_on", ASCENDING)], name="ix_joined")

db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("date", ASCENDING)],
    unique=True, name="uq_emp_date",
)
db.attendance_logs.create_index(
    [("date", DESCENDING), ("emp_code", ASCENDING)],
    name="ix_date_emp",
)
db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
    name="ix_emp_punch_in",
)

batch = []
total = 0

for employee_number in range(employee_count):
    emp_code = f"EMP{employee_number + 1:05d}"

    for day_number in range(day_count):
        work_date = start_date + timedelta(days=day_number)
        date_text = work_date.isoformat()

        # Deterministic mix of PRESENT, ABSENT, and LEAVE.
        choice = (employee_number + day_number) % 20
        status = "ABSENT" if choice == 0 else "LEAVE" if choice == 1 else "PRESENT"

        punch_in = None
        punch_out = None
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False

        if status == "PRESENT":
            # A mix of on-time and late arrivals, using IST shift times.
            late_by = 0 if choice % 4 == 0 else (choice % 7) * 5 + 12
            local_in = datetime.combine(
                work_date, time(9, 30), tzinfo=IST
            ) + timedelta(minutes=late_by)

            # A few hours after shift end for overtime variation.
            extra = (employee_number + day_number) % 4 * 15
            local_out = datetime.combine(
                work_date, time(18, 30), tzinfo=IST
            ) + timedelta(minutes=extra)

            punch_in = local_in.astimezone(UTC)
            punch_out = local_out.astimezone(UTC)
            work_hours = round((punch_out - punch_in).total_seconds() / 3600, 2)
            late_minutes = late_by if late_by > 10 else 0
            overtime_minutes = extra if extra >= 30 else 0
            half_day = work_hours < 4.5

        batch.append({
            "emp_code": emp_code,
            "date": date_text,
            "status": status,
            "punch_in": punch_in,
            "punch_out": punch_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
            "history": [],
        })

        if len(batch) >= 5000:
            db.attendance_logs.insert_many(batch, ordered=False)
            total += len(batch)
            print(f"Inserted {total:,} attendance records")
            batch.clear()

if batch:
    db.attendance_logs.insert_many(batch, ordered=False)
    total += len(batch)

print("Seeding complete.")
print("Database:", DB_NAME)
print("Employees:", db.employees.count_documents({}))
print("Attendance records:", db.attendance_logs.count_documents({}))
print("MongoDB server version:", client.server_info()["version"])

client.close()
