# FleetWise 🚚

**Vehicle & Delivery Assignment Dispatcher**  
Built for the Hackathon *"IT HAPPENS @ RAALE #13"*

FleetWise is a fast, dependency-light dispatch management system that guarantees **zero double-booking** under heavy concurrency. It combines atomic database transaction locks (`BEGIN IMMEDIATE`), partial unique indexes, an unified eligibility rules engine, and an interactive dispatcher interface.

---

## ⚡ Quick Start (Zero-Setup)

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Start the Server
```bash
uvicorn app.main:app --reload
```
The application will automatically initialize and seed `fleetwise.db` with demo vehicles, drivers, and deliveries on startup.

Open your browser to:
- **Dispatcher Dashboard**: [http://localhost:8000](http://localhost:8000)
- **Public Delivery Tracking**: [http://localhost:8000/track.html?id=D-201](http://localhost:8000/track.html?id=D-201)
- **API Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 🧪 Running the Tests

### Automated Test Suite (22 Unit & Concurrency Tests)
```bash
pytest -v
```

### Standalone Concurrency Benchmark
Fires 20 simultaneous threads contending for the same vehicle against a live server and prints a real-time ASCII result table:
```bash
python tests/concurrency_test.py
```

---

## 🏗️ System Architecture & Features

### Core Business Rules
- **Active Delivery Definition**: Status `assigned` OR `in_transit`.
- **Atomic Assignment Transaction**: Executed in a threadpool worker with `isolation_level=None` and `BEGIN IMMEDIATE`, ensuring that only one writer can assign a vehicle or driver at any time.
- **Defence-in-Depth Indexes**:
  ```sql
  CREATE UNIQUE INDEX one_active_vehicle ON assignments(vehicle_id) WHERE active=1;
  CREATE UNIQUE INDEX one_active_driver ON assignments(driver_id) WHERE active=1;
  CREATE UNIQUE INDEX one_active_delivery ON assignments(delivery_id) WHERE active=1;
  ```
- **Idempotency Keys**: Assign requests accept an optional `Idempotency-Key` header (generated via `crypto.randomUUID()` in the UI) to guarantee safe double-clicks without side-effects.
- **Proof of Delivery (OTP)**: When a delivery transitions to `in_transit`, a 4-digit numeric OTP is generated. Marking the order `delivered` strictly requires providing the matching OTP.
- **Breakdown Swap**: Atomically marks a broken vehicle as `maintenance`, frees its assignment, and re-allocates a fitting replacement vehicle to the same delivery and driver.
- **Shared Rules Engine**: All eligibility logic lives in `app/rules.py` and is shared by `/assign`, `/eligibility`, `/suggest`, and breakdown swap.

---

## 📁 Project Layout
```
fleetwise/
├── app/
│   ├── db.py                 # SQLite schema, connection factory, and seeding
│   ├── rules.py              # Unified eligibility and status transition rules
│   └── main.py               # FastAPI application with threadpool endpoints
├── static/
│   ├── index.html            # Dispatcher UI with Chaos button & live stats
│   └── track.html            # Public tracking page with progress stepper
├── tests/
│   ├── test_rules.py         # Required tests T1-T12 including concurrency
│   ├── test_extras.py        # 10 tests for capacity, service, OTP, CSV, etc.
│   ├── concurrency_test.py   # Standalone 20-thread concurrency benchmark
│   └── test_results.txt      # Real console test evidence
├── pytest.ini                # Pytest configuration
├── requirements.txt          # Python dependencies
├── REPORT.md                 # Technical hackathon report
└── README.md                 # Project documentation
```
