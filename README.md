# Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, attendance regularization, and analytics, developed using **Python, FastAPI, and MongoDB** as part of the HROne Software Engineer Trainee assignment.

## Table of Contents

- [Overview](#overview)
- [Technology Stack](#technology-stack)
- [Features](#features)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation and Setup](#installation-and-setup)
- [Environment Configuration](#environment-configuration)
- [Running the Application](#running-the-application)
- [API Endpoints](#api-endpoints)
- [Business Rules](#business-rules)
- [Database Design and Indexes](#database-design-and-indexes)
- [Testing](#testing)
- [MongoDB Explain Plans](#mongodb-explain-plans)
- [Sample Data](#sample-data)
- [Error Handling](#error-handling)
- [Project Documentation](#project-documentation)
- [Security](#security)

## Overview

The Employee Attendance & Analytics API provides endpoints to manage employees, record attendance, correct attendance records, and generate monthly and department-level analytics.

The application uses FastAPI for REST endpoints and MongoDB for storing employee and attendance data. MongoDB aggregation pipelines support the analytics endpoints, while database indexes support query performance.

The implementation follows the endpoint contract and business rules documented in `openapi.yaml`.

## Technology Stack

- **Language:** Python 3.11+
- **API Framework:** FastAPI
- **Database:** MongoDB 6.0+
- **Database Driver:** PyMongo
- **Data Validation:** Pydantic
- **Testing:** pytest, FastAPI TestClient, mongomock
- **Environment Configuration:** python-dotenv

## Features

### Employee Management

- Create employee records.
- Retrieve employee records with pagination.
- Filter employees by department.
- Validate employee details and shift times.
- Enforce unique employee codes using a MongoDB unique index.

### Attendance Management

- Record employee punch-in and punch-out timestamps.
- Support attendance statuses: `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, and `ON_DUTY`.
- Calculate work hours, late-arrival minutes, overtime, and half-day status.
- Support overnight shifts.
- Retrieve attendance records with filters and pagination.
- Prevent duplicate attendance records for the same employee and date.

### Attendance Regularization

- Correct existing attendance records.
- Update attendance status and punch timestamps.
- Require a correction reason and the identity of the person making the correction.
- Recalculate derived attendance values after corrections.
- Maintain an audit history of regularization changes.

### Analytics

- Employee monthly attendance summary.
- Department-wise monthly attendance summary.
- Late-arrival leaderboard.
- Department daily attendance trend.
- Seven-day moving average for attendance rates.
- MongoDB execution-plan inspection through the explain endpoint.

## Project Structure

```text
hrone-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── tests/
│   └── test_app.py
├── .env.example
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
├── requirements-dev.txt
├── sample_seed.py
├── load_test_seed.py
└── README.md
```

## Prerequisites

Install or configure the following:

- Python 3.11 or newer.
- MongoDB 6.0 or newer.
- Git.
- A MongoDB database and suitable database-user permissions.
- Network access to the MongoDB deployment.

MongoDB Atlas or a local MongoDB instance can be used.

## Installation and Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/hrone-attendance-analytics-api.git
cd hrone-attendance-analytics-api
```

Replace `YOUR_GITHUB_USERNAME` with your GitHub username.

### 2. Create and activate a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**macOS/Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

For the application:

```bash
pip install -r requirements.txt
```

For development and testing:

```bash
pip install -r requirements-dev.txt
```

### 4. Configure environment variables

Create a local `.env` file in the project root, using `.env.example` as a template.

For a local MongoDB instance:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For MongoDB Atlas, set your connection string locally in `.env`:

```dotenv
MONGO_URI=<your-mongodb-atlas-connection-string>
MONGO_DB=attendance_db
```

Replace the placeholder locally with your Atlas connection string. Do not put real credentials in this README or commit them to GitHub.

### Environment Variables

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB` | Name of the database used by the application |

Environment variables supplied by the operating system take precedence over values in `.env`.

Never commit the actual `.env` file or database credentials to GitHub.

## Running the Application

Start the application from the project root:

```bash
uvicorn app.main:app --reload --port 8000
```

The application will be available at:

- **Swagger UI:** http://127.0.0.1:8000/docs
- **ReDoc:** http://127.0.0.1:8000/redoc
- **Health check:** http://127.0.0.1:8000/health

The application creates the required MongoDB indexes at startup.

## API Endpoints

### System

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Check application and database readiness |

### Employees

| Method | Endpoint | Description |
|---|---|---|
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees with optional department filtering and pagination |

Example employee request:

```json
{
  "emp_code": "EMP3781",
  "name": "Tahseen Test",
  "email": "tahseen.test@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05"
}
```

### Attendance

| Method | Endpoint | Description |
|---|---|---|
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | Retrieve attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize an attendance record |

Attendance timestamps are provided as Unix epoch milliseconds.

Example punch-in request:

```json
{
  "emp_code": "EMP3781",
  "punched_at": 1791883800000,
  "status": "PRESENT"
}
```

Example punch-out request:

```json
{
  "emp_code": "EMP3781",
  "punched_at": 1791916200000
}
```

Example regularization request:

```json
{
  "status": "WFH",
  "reason": "Correcting attendance status",
  "regularized_by": "hr.admin"
}
```

Use the employee code and attendance date of an existing record when regularizing attendance.

### Analytics

| Method | Endpoint | Description |
|---|---|---|
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly attendance statistics for an employee |
| GET | `/analytics/departments/summary` | Monthly department statistics |
| GET | `/analytics/leaderboard/late` | Rank employees by late-arrival minutes |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend for a department |

Example requests:

```text
GET /analytics/employees/EMP3781/monthly?month=2026-10

GET /analytics/departments/summary?month=2026-10

GET /analytics/leaderboard/late?month=2026-10&limit=10

GET /analytics/departments/Engineering/trend?from=2026-10-01&to=2026-10-31
```

The department trend endpoint returns one row per calendar day in the requested range, including weekends and dates without attendance records. The maximum supported range is 92 calendar days, inclusive.

### MongoDB Explain

| Method | Endpoint | Description |
|---|---|---|
| GET | `/admin/explain/{endpoint}` | Inspect MongoDB execution plans and execution statistics |

Supported endpoint names:

- `attendance_list`
- `employee_monthly`
- `department_summary`
- `late_leaderboard`
- `department_trend`

The explain endpoint accepts the relevant query parameters for the endpoint being inspected. For example, `employee_monthly` requires `emp_code` and `month`, while `department_trend` requires `department`, `from`, and `to`.

Refer to Swagger UI and `openapi.yaml` for the complete request and response specifications.

## Business Rules

The implementation follows the assignment's documented rules, R1–R10.

- **R1 — Attendance date:** Attendance dates follow the specified IST calendar-date rules, including overnight shifts.
- **R2 — Late arrival:** An arrival is late when it is more than the 10-minute grace period after shift start.
- **R3 — Overtime:** Overtime is counted only when the time after shift end reaches at least 30 minutes. The duration is measured in whole minutes.
- **R4 — Work hours:** Work hours are calculated from punch-in and punch-out timestamps and rounded to two decimal places using half-up rounding. The value remains `null` until punch-out.
- **R5 — Half day:** After punch-out, `half_day` is true when the rounded work duration is below 4.50 hours. A half day contributes 0.5 to present days.
- **R6 — Presence:** `PRESENT`, `WFH`, and `ON_DUTY` count as presence. `ABSENT` and `LEAVE` do not.
- **R7 — Working days:** Monday through Friday are working days. There is no holiday calendar. Working days are counted from the employee's joining date when applicable. Weekend attendance can contribute to late and overtime totals.
- **R8 — Rounding:** Reported numbers use two decimal places with half-up rounding, while rates use four decimal places with half-up rounding.
- **R9 — Headcount:** Employees whose joining date is on or before the relevant reporting date are included, even if they have no attendance records.
- **R10 — Pagination:** Pages start at 1. Page size is limited to 100, and `total` reflects the filtered result set.

The complete endpoint contract and response schemas are defined in `openapi.yaml`.

## Database Design and Indexes

The application uses two primary MongoDB collections.

### `employees`

Stores employee information, department, shift schedule, joining date, and creation timestamp.

### `attendance_logs`

Stores daily attendance status, punch timestamps, calculated attendance fields, and regularization audit history.

### Indexes

The application creates indexes to enforce uniqueness and support common queries.

| Collection | Index | Purpose |
|---|---|---|
| `employees` | Unique `emp_code` | Enforce unique employee codes |
| `employees` | `(department, joined_on)` | Support department and joining-date queries |
| `employees` | `joined_on` | Support joining-date filtering |
| `attendance_logs` | Unique `(emp_code, date)` | Prevent duplicate daily attendance records |
| `attendance_logs` | `(date DESC, emp_code ASC)` | Support date-based attendance queries |
| `attendance_logs` | `(emp_code ASC, punch_in DESC)` | Support employee punch-in lookup |

Indexes are created idempotently during application startup.

## Testing

### Automated Tests

Run the test suite:

```bash
python -m pytest -q tests
```

The reported local test run produced:

```text
.................                                                        [100%]

17 passed, 1 warning in 2.47s
```

All 17 automated tests passed. One `StarletteDeprecationWarning` was reported concerning the use of `httpx` with `starlette.testclient`. It did not cause the tests to fail.

### Real MongoDB and Swagger Testing

The following endpoint responses were reported during Swagger UI testing against the configured MongoDB connection:

| Endpoint | Reported HTTP status |
|---|---:|
| `POST /employees` | 201 |
| `POST /attendance/punch-in` | 201 |
| `POST /attendance/punch-out` | 200 |
| `GET /attendance` | 200 |
| `GET /analytics/employees/{emp_code}/monthly` | 200 |
| `GET /analytics/departments/summary` | 200 |
| `GET /analytics/leaderboard/late` | 200 |
| `GET /analytics/departments/{department}/trend` | 200 |
| `GET /admin/explain/attendance_list` | 200 |
| `GET /admin/explain/employee_monthly` | 200 (tested with required parameters) |
| `GET /admin/explain/department_summary` | 200 |
| `GET /admin/explain/late_leaderboard` | 200 |
| `GET /admin/explain/department_trend` | 200 |

The tests included employee creation, punch-in, punch-out, attendance listing, monthly analytics, department summaries, the late leaderboard, department trends, and MongoDB explain inspection. The API and explain endpoints were tested against the MongoDB Atlas database that contained approximately 100,000 attendance records at the time of testing.

## MongoDB Explain Plan Results

The explain endpoint executes the main query or aggregation pipeline used by the corresponding API endpoint with MongoDB's `executionStats` verbosity.

The five explain endpoint types were tested against the Atlas database containing approximately 100,000 attendance records. The inspected results shared during testing included:

- **Attendance list:** `IXSCAN` using `ix_date_emp`, with no collection scans reported in the inspected plan.
- **Department summary:** Index usage was reported for the employee query and attendance lookup, with zero collection scans reported in the inspected plan.
- **Late leaderboard:** Index usage was reported in the inspected execution stages.
- **Department trend:** `IXSCAN` using `ix_date_emp` was observed in the inspected plan.
- **Employee monthly:** The explain endpoint was tested with its required `emp_code` and `month` parameters. Record its specific winning-plan details only if you have verified them in the saved response.

These are observations from the plans inspected. Check each endpoint's winning plan and execution statistics before claiming that every query uses an index or avoids `COLLSCAN`.

### Large-Dataset Testing

The API and the five explain endpoint types were tested against MongoDB Atlas after approximately 100,000 attendance records had been inserted into that database. The large dataset was already present during this testing; this README does not claim a separate local load test.

The reported checks covered attendance listing, employee monthly analytics, department summary, late leaderboard, and department trend. The recorded explain-plan observations are listed above.

No latency, throughput, or comparative performance benchmark is claimed unless those metrics were separately measured and recorded.

## Sample Data

The assignment provides:

- `sample_data/employees.json`
- `sample_data/attendance_logs.json`

The supplied files contain six employees and nine attendance records for functional testing.

The optional `sample_seed.py` script loads the sample data into MongoDB.

**Warning:** The sample seed script deletes existing documents in both collections before inserting the sample records. Run it only against a dedicated test database. Do not run it against a database containing data you need to preserve.

## Error Handling

The API uses FastAPI HTTP error responses for validation and operational failures.

Common status codes include:

| Status | Meaning |
|---|---|
| `200 OK` | Successful retrieval or update |
| `201 Created` | Successful creation |
| `404 Not Found` | Requested employee, attendance record, or department not found |
| `409 Conflict` | Duplicate record or conflicting update |
| `422 Unprocessable Entity` | Invalid request or business-rule violation |
| `503 Service Unavailable` | Database readiness or connectivity failure |

The actual response depends on the endpoint and the error encountered. Validation errors use FastAPI's `detail` response format.

## Project Documentation

The repository includes:

- **`openapi.yaml`** — API specification, schemas, endpoints, and business rules.
- **`DATA_MODEL.md`** — MongoDB collections, field types, and data-model requirements.
- **`DECISIONS.md`** — Design decisions and trade-offs.
- **`REVIEW.md`** — Review of starter-code issues and their fixes.
- **`README.md`** — Setup, endpoints, testing, and project documentation.

Interactive API documentation is available through Swagger UI and ReDoc when the application is running.

## Security

- Never commit `.env` or real database credentials.
- Keep `.env.example` limited to placeholder values.
- Use a dedicated test database for seed and load-test scripts.
- Restrict MongoDB network access to trusted IP addresses.
- Use suitable database-user permissions.
- Do not expose connection strings in screenshots, logs, or public repository content.

## Project Status

- **Automated tests:** 17 passed, with one non-blocking Starlette deprecation warning in the latest reported run.
- **Live API testing:** Employee, attendance, analytics, and explain endpoints were tested through Swagger UI against MongoDB Atlas.
- **Large dataset:** Approximately 100,000 attendance records were present in the Atlas database during the reported API and explain-plan testing.
- **Explain plans:** Inspected results are summarized in the MongoDB Explain Plan Results section. The specific winning-plan result for each query should be checked against its saved response before making a blanket claim about index usage.
- **Performance metrics:** No latency or throughput benchmark is claimed unless measured separately and recorded.
