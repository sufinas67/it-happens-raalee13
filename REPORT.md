# FleetWise Project Report — Hackathon "IT HAPPENS @ RAALE #13"

## 1. What We Built
**FleetWise** is a robust, lightweight Vehicle & Delivery Assignment System engineered for high-concurrency environments with strict zero-double-booking guarantees. It delivers an end-to-end dispatcher platform that eliminates resource race conditions, provides proactive matching and queue prioritization, supports dynamic breakdown recovery, and provides instant recipient delivery verification via 4-digit OTPs.

The system features:
- **Zero-Setup Startup**: Self-initializing SQLite database auto-seeded on first run.
- **Defence-in-Depth Concurrency Protection**: Atomic transactions using `BEGIN IMMEDIATE` combined with SQLite partial unique indexes.
- **Single Source of Truth**: All eligibility rules (capacity, service overdue dates, shift limits, availability statuses) live in one function (`app/rules.py`) shared by assign, eligibility ("why not?"), suggest, and breakdown swap.
- **Dispatcher UI**: A single-file responsive dashboard (`static/index.html`) featuring live utilization metrics, "Why not?" disabled dropdowns with blocker reasons, audit timeline viewer, one-click priority queue matching, and an in-browser Chaos Button firing 20 simultaneous competing requests.
- **Public Tracking Stepper**: A public page (`static/track.html?id=...`) providing a real-time progress stepper without leaking internal driver phone numbers or OTP secrets.

---

## 2. Data Model
FleetWise uses raw SQLite with foreign keys enabled, WAL journal mode, and a busy timeout of 5,000 ms.

### Entities & Schemas
- **`vehicles`**:
  - `vehicle_id` (TEXT, PK): Unique vehicle identifier (e.g., `V-101`).
  - `registration_no` (TEXT, UNIQUE, NOT NULL): Official license plate.
  - `type` (TEXT, NOT NULL): Van, Truck, Bike, etc.
  - `capacity` (INTEGER, NOT NULL): Maximum cargo load in kg (`> 0`).
  - `status` (TEXT, CHECK `status IN ('available', 'assigned', 'maintenance', 'inactive')`).
  - `service_due_date` (TEXT, NOT NULL): Due date in `YYYY-MM-DD` format.
- **`drivers`**:
  - `driver_id` (TEXT, PK): Driver identifier (e.g., `DR-001`).
  - `name` (TEXT, NOT NULL), `phone` (TEXT, NOT NULL).
  - `availability_status` (TEXT, CHECK `availability_status IN ('available', 'assigned', 'unavailable')`).
  - `max_deliveries_per_shift` (INTEGER, DEFAULT 5).
  - `deliveries_this_shift` (INTEGER, DEFAULT 0).
- **`deliveries`**:
  - `delivery_id` (TEXT, PK): Delivery ID (e.g., `D-201`).
  - `pickup` (TEXT, NOT NULL), `dropoff` (TEXT, NOT NULL), `scheduled_time` (TEXT, NOT NULL).
  - `priority` (TEXT, CHECK `priority IN ('low', 'normal', 'high')`).
  - `load_kg` (INTEGER, NOT NULL, `> 0`).
  - `status` (TEXT, CHECK `status IN ('pending', 'assigned', 'in_transit', 'delivered', 'cancelled')`).
  - `customer_name` (TEXT, NOT NULL).
  - `otp` (TEXT, NULLABLE): 4-digit verification code generated when transitioning to `in_transit`.
  - `created_at` (TEXT, NOT NULL).
- **`assignments`**:
  - `assignment_id` (INTEGER, PK AUTOINCREMENT).
  - `delivery_id` (TEXT, NOT NULL), `vehicle_id` (TEXT, NOT NULL), `driver_id` (TEXT, NOT NULL).
  - `assigned_at` (TEXT, NOT NULL), `released_at` (TEXT, NULLABLE).
  - `active` (INTEGER, DEFAULT 1): Flag indicating if assignment is currently active (`1`) or closed (`0`).
- **`audit_log`**:
  - `id` (INTEGER, PK AUTOINCREMENT), `ts` (TEXT), `actor` (TEXT), `delivery_id` (TEXT), `event` (TEXT), `detail` (TEXT).
- **`idempotency_keys`**:
  - `key` (TEXT, PK), `response_json` (TEXT), `status_code` (INTEGER), `created_at` (TEXT).

### Database Partial Unique Indexes (Defence-in-Depth)
```sql
CREATE UNIQUE INDEX one_active_vehicle ON assignments(vehicle_id) WHERE active=1;
CREATE UNIQUE INDEX one_active_driver ON assignments(driver_id) WHERE active=1;
CREATE UNIQUE INDEX one_active_delivery ON assignments(delivery_id) WHERE active=1;
```

### Allowed Status Transitions
1. `pending -> assigned` (Permitted ONLY via `/deliveries/{id}/assign`).
2. `pending -> cancelled`.
3. `assigned -> in_transit`.
4. `assigned -> cancelled`.
5. `in_transit -> delivered` (Requires correct recipient OTP).
*Note: Deliveries `in_transit` cannot be cancelled.*

---

## 3. Assignment Rule & Concurrency Architecture

### Transaction Execution with `BEGIN IMMEDIATE`
Standard SQLite transactions start in `DEFERRED` mode, which acquires read locks and only attempts to upgrade to write locks on the first mutation. Under high concurrency, multiple threads can read available resources simultaneously and then deadlock or race when trying to upgrade locks.

FleetWise configures connections with `isolation_level=None` and runs `BEGIN IMMEDIATE`. This instantly acquires a `RESERVED` lock on the database. Concurrent threads attempting `BEGIN IMMEDIATE` are paused up to the 5-second `busy_timeout`. When a second thread acquires the transaction lock:
1. It re-reads the fresh database state.
2. The server-side eligibility check discovers the vehicle or driver is already marked `assigned` or has an active row in `assignments`.
3. It aborts cleanly and returns HTTP 409 Conflict with a descriptive blocker error message (e.g., `"Vehicle V-101 is currently assigned"`).
4. If an unexpected condition bypassed validation, the partial unique indexes reject the insert with `sqlite3.IntegrityError`, caught and converted to HTTP 409 Conflict.

### Idempotency Key Handling
Each assignment click from the UI generates a `crypto.randomUUID()` transmitted in the `Idempotency-Key` header.
- Upon receiving a key, the endpoint checks `idempotency_keys`. If found, the exact original response body and HTTP 201 status are served without reprocessing or creating secondary assignments.
- Subsequent requests without the key (or with a different key) evaluate against the updated state and return HTTP 409.

### Real Concurrency Benchmark Results
In both `test_t12_concurrency` and standalone `tests/concurrency_test.py`:
- **20 simultaneous threads** contested the **SAME vehicle** (`V-101`) with distinct drivers and deliveries.
- **Results**: **Exactly 1 request succeeded (HTTP 201)** and **19 requests were rejected (HTTP 409)**.
- **DB Verification**: Exactly 1 active assignment record was present in the database. Zero double-booking occurred.

---

## 4. Methods and Decisions

### Chosen Stack
- **Python 3.11+ & FastAPI**: Clean, type-safe API definition with automatic OpenAPI docs and high performance.
- **Plain synchronous `def` endpoints**: FastAPI runs regular `def` functions in the `anyio` worker threadpool. This ensures true multi-threaded concurrent OS execution against SQLite's file-locking layer, rather than interleaving within an async event loop on a single thread.
- **Raw `sqlite3`**: Zero ORM overhead, full control over WAL mode, busy timeout, isolation levels, and explicit transaction control (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`).
- **Vanilla HTML/JS/CSS**: Single static file with zero build step, zero npm dependencies, instant loading, and native browser APIs (`fetch`, `crypto.randomUUID`, `FormData`).

### Rejected Alternatives
- **ORM (SQLAlchemy / Tortoise)**: Rejected because ORM unit-of-work patterns obscure low-level SQLite lock transitions and transaction boundaries, making strict concurrency benchmarking unpredictable.
- **PostgreSQL / MySQL**: Rejected to satisfy Priority 2 (Zero-setup run). A self-contained SQLite file requires zero Docker containers or database server installations.
- **React / Vue / Vite**: Rejected to eliminate build artifacts, npm dependencies, node runtime prerequisites, and bundling delays.
- **Async DB drivers (`aiosqlite`)**: Rejected because async SQLite runs on a background thread anyway, while plain `def` endpoints in FastAPI execute concurrently across threadpool workers with direct OS thread semantics.

---

## 5. Results Table
All test results below are extracted directly from the actual execution of `tests/test_results.txt`:

| Test Name | Target Input / Scenario | Expected Behavior | Observed Result | Pass / Fail |
|:---|:---|:---|:---|:---:|
| `test_capacity_too_small_rejected` | Assign D-203 (1200kg) to V-101 (500kg cap) | 409 VEHICLE_TOO_SMALL, DB unchanged | 409 VEHICLE_TOO_SMALL, DB snapshot identical | **PASSED** |
| `test_overdue_service_rejected` | Assign D-202 to V-105 (service due 2025-01-10) | 409 VEHICLE_SERVICE_OVERDUE, DB unchanged | 409 VEHICLE_SERVICE_OVERDUE, DB snapshot identical | **PASSED** |
| `test_shift_limit_rejected` | Assign D-204 to DR-004 after completing 5/5 shifts | 409 DRIVER_SHIFT_LIMIT, DB unchanged | 409 DRIVER_SHIFT_LIMIT, DB snapshot identical | **PASSED** |
| `test_wrong_otp_rejected` | Patch D-202 in_transit to delivered with bad OTP | 409 INVALID_OTP, state stays in_transit | 409 INVALID_OTP, DB snapshot identical | **PASSED** |
| `test_breakdown_swap_success` | V-103 breaks down on active delivery D-204 | 200 OK, V-103 maintenance, new vehicle assigned | 200 OK, V-103 maintenance, replacement active | **PASSED** |
| `test_breakdown_swap_no_replacement` | V-102 (2000kg) breaks down on 1800kg load; no replacement | 409 VEHICLE_UNAVAILABLE, DB unchanged | 409 VEHICLE_UNAVAILABLE, DB snapshot identical | **PASSED** |
| `test_suggest_returns_smallest_fitting` | D-204 (15kg) and D-201 (350kg) | Suggests smallest fitting vehicle (V-103 40kg, V-101 500kg) | Correct smallest vehicle & lowest shift driver | **PASSED** |
| `test_queue_ordering_by_priority` | GET /queue with 8 pending deliveries | High priority first, then normal, then low | Correctly sorted by priority rank & scheduled time | **PASSED** |
| `test_csv_export_works` | GET /export/deliveries.csv & /export/audit.csv | 200 OK with valid text/csv content & headers | 200 OK, valid headers and seeded records | **PASSED** |
| `test_db_partial_unique_index_rejects_raw` | Direct SQL active duplicate inserts | sqlite3.IntegrityError on active duplicate | sqlite3.IntegrityError raised; inactive succeeds | **PASSED** |
| `test_t1_valid_assignment` | Assign D-202 to V-101 & DR-001 | 201 Created, all entities updated to assigned | 201 Created, vehicle, driver & delivery updated | **PASSED** |
| `test_t2_vehicle_conflict` | Assign V-101 to D-202 then to D-204 | 409 VEHICLE_UNAVAILABLE naming V-101, DB unchanged | 409 VEHICLE_UNAVAILABLE, DB snapshot identical | **PASSED** |
| `test_t3_driver_conflict` | Assign DR-001 to D-202 then to D-204 | 409 DRIVER_UNAVAILABLE naming DR-001, DB unchanged | 409 DRIVER_UNAVAILABLE, DB snapshot identical | **PASSED** |
| `test_t4_both_occupied` | Assign V-101 & DR-001 to D-202 then D-204 | 409 conflict, DB unchanged | 409 conflict, DB snapshot identical | **PASSED** |
| `test_t5_unavailable_driver` | Assign D-202 to unavailable DR-003 | 409 DRIVER_UNAVAILABLE, DB unchanged | 409 DRIVER_UNAVAILABLE, DB snapshot identical | **PASSED** |
| `test_t6_maintenance_vehicle` | Assign D-202 to maintenance V-104 | 409 VEHICLE_UNAVAILABLE, DB unchanged | 409 VEHICLE_UNAVAILABLE, DB snapshot identical | **PASSED** |
| `test_t7_unknown_ids_404` | Assign non-existent D-9999, V-9999, DR-9999 | 404 NOT_FOUND, no server crash, DB unchanged | 404 NOT_FOUND on all 3, DB snapshot identical | **PASSED** |
| `test_t8_delivered_releases_resources` | Assign -> in_transit (OTP) -> deliver with OTP | 200 OK, vehicle & driver available, shift count +1 | 200 OK, resources released, shift count incremented | **PASSED** |
| `test_t9_cancel_releases_resources` | Assign -> cancel from assigned | 200 OK, vehicle & driver available, shift count unchanged | 200 OK, resources released, shift count preserved | **PASSED** |
| `test_t10_pending_to_delivered_rejected` | Patch pending delivery directly to delivered | 409 INVALID_TRANSITION, DB unchanged | 409 INVALID_TRANSITION, DB snapshot identical | **PASSED** |
| `test_t11_duplicate_submit_idempotency` | Assign with Idempotency-Key twice, then without key | 1st 201, 2nd cached 201, 3rd 409 conflict | Cached identical 201, exactly 1 active DB row, 3rd 409 | **PASSED** |
| `test_t12_concurrency` | 20 parallel threads contending for V-101 | Exactly 1 success (201), 19 conflicts (409) | Exactly 1 success (201), 19 conflicts (409) | **PASSED** |
| `concurrency_test.py` (Standalone) | 20 parallel requests against live server for V-101 | 1 success (201), 19 conflicts (409), 0 errors | 1 success (201), 19 conflicts (409), 0 errors | **PASSED** |

**Summary**: 22 pytest unit/integration tests + 1 standalone concurrency load test executed. **All 23 tests PASSED (100% success rate)**.

---

## 6. Team Work
*(Placeholders for hackathon submission details — do not remove)*

- **Team Members & Role Ownership**:
  - `[Owner 1 Name / Role]`: Core backend architecture, SQLite transactions & concurrency lock isolation.
  - `[Owner 2 Name / Role]`: Eligibility rules engine, suggestion algorithm, & test suite automation.
  - `[Owner 3 Name / Role]`: Dispatcher UI, real-time stats bar, "Why not?" panel & public tracking page.
- **Planned vs. Actual Timeline**:
  - `[Planned]`: Phase 1 Database & Rules (30m) -> Phase 2 API Endpoints (30m) -> Phase 3 UI & Extras (40m) -> Phase 4 Verification (20m).
  - `[Actual]`: Phase 1 & 2 Completed in 45m. Concurrency isolation required fine-tuning SQLite busy timeout and connection isolation level. Full suite T1-T12 and extras passed cleanly within 1h 15m.
- **Dead End Encountered**:
  - `[Dead End Description]`: Initially considered using an in-memory SQLite database (`:memory:`) for multi-threaded testing. However, distinct threadpool connections to `:memory:` create independent isolated in-memory instances rather than sharing a single physical store, defeating cross-thread concurrency testing. Switching to a temporary file-backed SQLite database with `PRAGMA journal_mode = WAL` and `PRAGMA busy_timeout = 5000` provided the exact concurrent file-locking behavior needed for realistic validation.

---

## 7. Limitations and Next Steps
1. **Authentication & Authorization**: FleetWise is designed for trusted internal dispatchers during this hackathon demonstration. Future iterations should add JWT/OAuth2 role-based access control (Dispatcher vs Driver vs Public).
2. **SQLite Single-Writer Concurrency**: While SQLite handles concurrent readers and serializes writers cleanly with WAL and busy timeouts up to moderate loads, high-scale enterprise fleets handling thousands of simultaneous writes per second would eventually migrate to PostgreSQL with `SELECT ... FOR UPDATE`.
3. **Delivery Scheduling Windows**: An "active" delivery is strictly defined as `assigned` or `in_transit`. Vehicles cannot currently be pre-allocated to future time slots while servicing another delivery. A calendar time-slot reservation engine would be the natural extension.
4. **OTP Exposure**: Recipient delivery OTPs are shown in the dispatcher UI for demonstration and testing purposes. In production, OTPs would be transmitted strictly via SMS/WhatsApp to the customer's phone and kept confidential from the dispatcher.

---

## 8. How to Run
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the application
uvicorn app.main:app --reload

# 3. Run test suite
pytest -v

# 4. Run standalone concurrency benchmark
python tests/concurrency_test.py
```
Open **http://localhost:8000** in your browser to access the live dispatcher UI.
