import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import httpx
from fastapi.testclient import TestClient

from app.db import init_db, seed_db, get_db_snapshot, get_db_connection
from app.main import app

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port

@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_fleetwise.db")
    monkeypatch.setenv("FLEETWISE_DB_PATH", db_file)
    init_db(db_file)
    seed_db(db_file, force=True)
    return db_file

@pytest.fixture
def client(test_db):
    return TestClient(app)

def test_t1_valid_assignment(client, test_db):
    # D-202 (25kg) with V-101 (500kg) and DR-001
    resp = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["delivery_id"] == "D-202"
    assert data["vehicle_id"] == "V-101"
    assert data["driver_id"] == "DR-001"
    assert data["status"] == "assigned"

    # Verify DB states
    conn = get_db_connection(test_db)
    deliv = conn.execute("SELECT status FROM deliveries WHERE delivery_id = 'D-202'").fetchone()
    assert deliv["status"] == "assigned"

    veh = conn.execute("SELECT status FROM vehicles WHERE vehicle_id = 'V-101'").fetchone()
    assert veh["status"] == "assigned"

    dr = conn.execute("SELECT availability_status FROM drivers WHERE driver_id = 'DR-001'").fetchone()
    assert dr["availability_status"] == "assigned"

    assign = conn.execute("SELECT * FROM assignments WHERE delivery_id = 'D-202' AND active = 1").fetchone()
    assert assign is not None
    assert assign["vehicle_id"] == "V-101"
    assert assign["driver_id"] == "DR-001"

    audit = conn.execute("SELECT * FROM audit_log WHERE delivery_id = 'D-202' AND event = 'ASSIGNED'").fetchone()
    assert audit is not None
    conn.close()

def test_t2_vehicle_conflict(client, test_db):
    # Assign V-101 to D-202 first
    resp1 = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert resp1.status_code == 201

    snap_before = get_db_snapshot(test_db)

    # Try assigning same V-101 to D-204
    resp2 = client.post("/deliveries/D-204/assign", json={"vehicle_id": "V-101", "driver_id": "DR-002"})
    assert resp2.status_code == 409
    data = resp2.json()
    assert data["error"] == "VEHICLE_UNAVAILABLE"
    assert "V-101" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t3_driver_conflict(client, test_db):
    # Assign DR-001 to D-202
    resp1 = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert resp1.status_code == 201

    snap_before = get_db_snapshot(test_db)

    # Try assigning DR-001 to D-204 with another vehicle V-102
    resp2 = client.post("/deliveries/D-204/assign", json={"vehicle_id": "V-102", "driver_id": "DR-001"})
    assert resp2.status_code == 409
    data = resp2.json()
    assert data["error"] == "DRIVER_UNAVAILABLE"
    assert "DR-001" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t4_both_occupied(client, test_db):
    # Assign V-101 and DR-001 to D-202
    resp1 = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert resp1.status_code == 201

    snap_before = get_db_snapshot(test_db)

    # Try assigning both occupied resources to D-204
    resp2 = client.post("/deliveries/D-204/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert resp2.status_code == 409
    assert resp2.json()["error"] in ("VEHICLE_UNAVAILABLE", "DRIVER_UNAVAILABLE")

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t5_unavailable_driver(client, test_db):
    # DR-003 is seeded as unavailable
    snap_before = get_db_snapshot(test_db)

    resp = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-003"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "DRIVER_UNAVAILABLE"
    assert "DR-003" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t6_maintenance_vehicle(client, test_db):
    # V-104 is seeded as maintenance
    snap_before = get_db_snapshot(test_db)

    resp = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-104", "driver_id": "DR-001"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "VEHICLE_UNAVAILABLE"
    assert "V-104" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t7_unknown_ids_404(client, test_db):
    snap_before = get_db_snapshot(test_db)

    # Unknown delivery
    r1 = client.post("/deliveries/D-9999/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert r1.status_code == 404
    assert r1.json()["error"] == "NOT_FOUND"

    # Unknown vehicle
    r2 = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-9999", "driver_id": "DR-001"})
    assert r2.status_code == 404
    assert r2.json()["error"] == "NOT_FOUND"

    # Unknown driver
    r3 = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-9999"})
    assert r3.status_code == 404
    assert r3.json()["error"] == "NOT_FOUND"

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t8_delivered_releases_vehicle_and_driver(client, test_db):
    # Assign
    client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-002"})

    # Check driver starting shift count
    conn = get_db_connection(test_db)
    dr_before = conn.execute("SELECT deliveries_this_shift FROM drivers WHERE driver_id = 'DR-002'").fetchone()
    count_before = dr_before["deliveries_this_shift"]
    conn.close()

    # Move to in_transit
    r_transit = client.patch("/deliveries/D-202/status", json={"status": "in_transit"})
    assert r_transit.status_code == 200

    # Get OTP from deliveries endpoint
    r_deliv = client.get("/deliveries")
    deliv_obj = next(d for d in r_deliv.json() if d["delivery_id"] == "D-202")
    otp = deliv_obj["otp"]
    assert otp is not None
    assert len(otp) == 4

    # Move to delivered using OTP
    r_deliv_res = client.patch("/deliveries/D-202/status", json={"status": "delivered", "otp": otp})
    assert r_deliv_res.status_code == 200
    assert "next_suggestion" in r_deliv_res.json()

    # Verify vehicle and driver released
    conn = get_db_connection(test_db)
    veh = conn.execute("SELECT status FROM vehicles WHERE vehicle_id = 'V-101'").fetchone()
    assert veh["status"] == "available"

    dr = conn.execute("SELECT availability_status, deliveries_this_shift FROM drivers WHERE driver_id = 'DR-002'").fetchone()
    assert dr["availability_status"] == "available"
    assert dr["deliveries_this_shift"] == count_before + 1

    assign = conn.execute("SELECT active, released_at FROM assignments WHERE delivery_id = 'D-202'").fetchone()
    assert assign["active"] == 0
    assert assign["released_at"] is not None
    conn.close()

def test_t9_cancel_releases_resources(client, test_db):
    # Assign
    client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})

    # Cancel from assigned
    r_cancel = client.patch("/deliveries/D-202/status", json={"status": "cancelled"})
    assert r_cancel.status_code == 200
    assert "next_suggestion" in r_cancel.json()

    conn = get_db_connection(test_db)
    deliv = conn.execute("SELECT status FROM deliveries WHERE delivery_id = 'D-202'").fetchone()
    assert deliv["status"] == "cancelled"

    veh = conn.execute("SELECT status FROM vehicles WHERE vehicle_id = 'V-101'").fetchone()
    assert veh["status"] == "available"

    dr = conn.execute("SELECT availability_status FROM drivers WHERE driver_id = 'DR-001'").fetchone()
    assert dr["availability_status"] == "available"

    assign = conn.execute("SELECT active, released_at FROM assignments WHERE delivery_id = 'D-202'").fetchone()
    assert assign["active"] == 0
    assert assign["released_at"] is not None
    conn.close()

def test_t10_pending_to_delivered_rejected(client, test_db):
    snap_before = get_db_snapshot(test_db)

    # Try moving pending directly to delivered
    resp = client.patch("/deliveries/D-202/status", json={"status": "delivered", "otp": "1234"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "INVALID_TRANSITION"

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_t11_duplicate_submit_idempotency(client, test_db):
    headers = {"Idempotency-Key": "test-idempotency-key-001"}
    payload = {"vehicle_id": "V-101", "driver_id": "DR-001"}

    # 1. First submit with Idempotency-Key -> 201
    resp1 = client.post("/deliveries/D-202/assign", json=payload, headers=headers)
    assert resp1.status_code == 201
    body1 = resp1.json()

    # 2. Second submit with same Idempotency-Key -> returns identical 201 response
    resp2 = client.post("/deliveries/D-202/assign", json=payload, headers=headers)
    assert resp2.status_code == 201
    body2 = resp2.json()
    assert body1 == body2

    # Assert only 1 active assignment
    conn = get_db_connection(test_db)
    cur = conn.execute("SELECT COUNT(*) as cnt FROM assignments WHERE delivery_id = 'D-202' AND active = 1")
    assert cur.fetchone()["cnt"] == 1
    conn.close()

    # 3. Third submit without key -> rejected 409
    resp3 = client.post("/deliveries/D-202/assign", json=payload)
    assert resp3.status_code == 409
    assert resp3.json()["error"] in ("DELIVERY_NOT_PENDING", "VEHICLE_UNAVAILABLE", "DRIVER_UNAVAILABLE")

def test_t12_concurrency(tmp_path):
    # 20 threads against a real running uvicorn server on a free port
    import uvicorn

    db_path = str(tmp_path / "concurrency_test.db")
    os.environ["FLEETWISE_DB_PATH"] = db_path
    init_db(db_path)
    seed_db(db_path, force=True)

    port = get_free_port()
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # Wait for server to come up
    ready = False
    for _ in range(50):
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.1)
    assert ready, "Server failed to start in time"

    # We will try to assign the SAME vehicle V-101 across 20 requests
    # Deliveries: D-201 to D-208, plus extra deliveries if needed
    # Drivers: DR-001, DR-002, DR-004, DR-005
    conn = get_db_connection(db_path)
    # Insert 20 pending deliveries
    for i in range(1, 21):
        deliv_id = f"C-DELIV-{i}"
        conn.execute(
            "INSERT OR REPLACE INTO deliveries (delivery_id, pickup, dropoff, scheduled_time, priority, load_kg, status, customer_name, created_at) VALUES (?, 'P', 'D', '2026-10-30 10:00', 'normal', 50, 'pending', 'Customer', '2026-10-29 12:00:00')",
            (deliv_id,)
        )
    # Insert 20 available drivers
    for i in range(1, 21):
        dr_id = f"C-DR-{i}"
        conn.execute(
            "INSERT OR REPLACE INTO drivers (driver_id, name, phone, availability_status, max_deliveries_per_shift, deliveries_this_shift) VALUES (?, 'Driver', '12345', 'available', 5, 0)",
            (dr_id,)
        )
    conn.close()

    results = []
    def do_assign(i):
        deliv_id = f"C-DELIV-{i}"
        dr_id = f"C-DR-{i}"
        try:
            r = httpx.post(
                f"{base_url}/deliveries/{deliv_id}/assign",
                json={"vehicle_id": "V-101", "driver_id": dr_id},
                timeout=10.0
            )
            return r.status_code
        except Exception as e:
            return str(e)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(do_assign, i) for i in range(1, 21)]
        results = [f.result() for f in futures]

    success_count = results.count(201)
    conflict_count = results.count(409)

    # Clean shutdown of server
    server.should_exit = True
    server_thread.join(timeout=2.0)

    assert success_count == 1, f"Expected exactly 1 success, got {success_count}. Results: {results}"
    assert conflict_count == 19, f"Expected 19 conflicts, got {conflict_count}. Results: {results}"

    # Verify DB has exactly one active assignment for V-101
    conn = get_db_connection(db_path)
    active_assignments = conn.execute("SELECT * FROM assignments WHERE vehicle_id = 'V-101' AND active = 1").fetchall()
    assert len(active_assignments) == 1
    conn.close()
