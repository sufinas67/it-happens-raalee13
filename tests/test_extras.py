import sqlite3
import pytest
from fastapi.testclient import TestClient

from app.db import init_db, seed_db, get_db_snapshot, get_db_connection
from app.main import app

@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_extras_fleetwise.db")
    monkeypatch.setenv("FLEETWISE_DB_PATH", db_file)
    init_db(db_file)
    seed_db(db_file, force=True)
    return db_file

@pytest.fixture
def client(test_db):
    return TestClient(app)

def test_capacity_too_small_rejected(client, test_db):
    # D-203 has load_kg = 1200, V-101 has capacity = 500
    snap_before = get_db_snapshot(test_db)

    resp = client.post("/deliveries/D-203/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "VEHICLE_TOO_SMALL"
    assert "V-101" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_overdue_service_rejected(client, test_db):
    # V-105 is seeded with overdue service date (2025-01-10)
    snap_before = get_db_snapshot(test_db)

    resp = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-105", "driver_id": "DR-001"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "VEHICLE_SERVICE_OVERDUE"
    assert "V-105" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_shift_limit_rejected(client, test_db):
    # DR-004 is seeded with deliveries_this_shift = 4 of 5
    # First assign D-202 to DR-004 and complete it
    r_assign = client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-004"})
    assert r_assign.status_code == 201

    r_trans = client.patch("/deliveries/D-202/status", json={"status": "in_transit"})
    assert r_trans.status_code == 200

    r_deliv = client.get("/deliveries")
    otp = next(d for d in r_deliv.json() if d["delivery_id"] == "D-202")["otp"]

    r_comp = client.patch("/deliveries/D-202/status", json={"status": "delivered", "otp": otp})
    assert r_comp.status_code == 200

    # DR-004 now has 5/5 deliveries completed
    snap_before = get_db_snapshot(test_db)

    # Try assigning another delivery D-204 to DR-004
    resp = client.post("/deliveries/D-204/assign", json={"vehicle_id": "V-101", "driver_id": "DR-004"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "DRIVER_SHIFT_LIMIT"
    assert "DR-004" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_wrong_otp_rejected(client, test_db):
    client.post("/deliveries/D-202/assign", json={"vehicle_id": "V-101", "driver_id": "DR-001"})
    client.patch("/deliveries/D-202/status", json={"status": "in_transit"})

    snap_before = get_db_snapshot(test_db)

    # Attempt delivery with wrong OTP
    resp = client.patch("/deliveries/D-202/status", json={"status": "delivered", "otp": "999999"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "INVALID_OTP"

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_breakdown_swap_success(client, test_db):
    # D-204 (15kg) assigned to V-103 (Bike, 40kg) and DR-001
    r_assign = client.post("/deliveries/D-204/assign", json={"vehicle_id": "V-103", "driver_id": "DR-001"})
    assert r_assign.status_code == 201

    resp = client.post("/vehicles/V-103/breakdown")
    assert resp.status_code == 200
    data = resp.json()
    assert data["delivery_id"] == "D-204"
    assert data["old_vehicle_id"] == "V-103"
    assert data["driver_id"] == "DR-001"
    new_v_id = data["new_vehicle_id"]
    assert new_v_id != "V-103"
    assert "next_suggestion" in data

    # Verify DB state
    conn = get_db_connection(test_db)
    old_veh = conn.execute("SELECT status FROM vehicles WHERE vehicle_id = 'V-103'").fetchone()
    assert old_veh["status"] == "maintenance"

    new_veh = conn.execute("SELECT status FROM vehicles WHERE vehicle_id = ?", (new_v_id,)).fetchone()
    assert new_veh["status"] == "assigned"

    # Only 1 active assignment for D-204
    active_assign = conn.execute("SELECT * FROM assignments WHERE delivery_id = 'D-204' AND active = 1").fetchall()
    assert len(active_assign) == 1
    assert active_assign[0]["vehicle_id"] == new_v_id
    assert active_assign[0]["driver_id"] == "DR-001"

    # Closed assignment exists
    closed_assign = conn.execute("SELECT * FROM assignments WHERE vehicle_id = 'V-103' AND active = 0").fetchall()
    assert len(closed_assign) == 1
    conn.close()

def test_breakdown_swap_no_replacement_leaves_everything_unchanged(client, test_db):
    # D-206 has load_kg = 1800. Only V-102 (capacity 2000) can carry it.
    client.post("/deliveries/D-206/assign", json={"vehicle_id": "V-102", "driver_id": "DR-001"})

    snap_before = get_db_snapshot(test_db)

    # Breakdown for V-102 has no candidate vehicle with capacity >= 1800
    resp = client.post("/vehicles/V-102/breakdown")
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "VEHICLE_UNAVAILABLE"
    assert "No eligible replacement vehicle" in data["message"]

    snap_after = get_db_snapshot(test_db)
    assert snap_before == snap_after

def test_suggest_returns_smallest_fitting_vehicle(client, test_db):
    # D-204 has load_kg = 15. Smallest available fitting vehicle is V-103 (capacity 40).
    resp = client.get("/deliveries/D-204/suggest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["eligible"] is True
    assert data["suggested_vehicle"]["vehicle_id"] == "V-103"
    assert data["suggested_driver"]["driver_id"] == "DR-002" # 0 deliveries this shift

    # D-201 has load_kg = 350. V-103 (40kg) is too small. Next smallest is V-101 (500kg).
    resp2 = client.get("/deliveries/D-201/suggest")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["eligible"] is True
    assert data2["suggested_vehicle"]["vehicle_id"] == "V-101"

def test_queue_ordering_by_priority(client, test_db):
    resp = client.get("/queue")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 8

    priorities = [item["delivery"]["priority"] for item in items]
    # Check that high priorities come before normal, which come before low
    priority_weights = {"high": 1, "normal": 2, "low": 3}
    weights = [priority_weights[p] for p in priorities]
    assert weights == sorted(weights)

def test_csv_export_works(client, test_db):
    r_deliv = client.get("/export/deliveries.csv")
    assert r_deliv.status_code == 200
    assert r_deliv.headers["content-type"] == "text/csv; charset=utf-8"
    content = r_deliv.text
    assert "delivery_id,customer_name,pickup,dropoff" in content
    assert "FreshMart Retail" in content

    r_audit = client.get("/export/audit.csv")
    assert r_audit.status_code == 200
    assert r_audit.headers["content-type"] == "text/csv; charset=utf-8"
    audit_text = r_audit.text
    assert "id,ts,actor,delivery_id,event,detail" in audit_text
    assert "SEEDED" in audit_text

def test_db_partial_unique_index_rejects_duplicate_active_insert(test_db):
    conn = get_db_connection(test_db)
    # Direct raw insert into assignments
    conn.execute(
        "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES ('D-TEST-1', 'V-TEST-1', 'DR-TEST-1', '2026-10-29 12:00:00', 1)"
    )

    # Attempting duplicate active vehicle
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES ('D-TEST-2', 'V-TEST-1', 'DR-TEST-2', '2026-10-29 12:00:00', 1)"
        )

    # Attempting duplicate active driver
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES ('D-TEST-3', 'V-TEST-2', 'DR-TEST-1', '2026-10-29 12:00:00', 1)"
        )

    # Attempting duplicate active delivery
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES ('D-TEST-1', 'V-TEST-3', 'DR-TEST-3', '2026-10-29 12:00:00', 1)"
        )

    # But non-active duplicate IS allowed (historical records)
    conn.execute(
        "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES ('D-TEST-1', 'V-TEST-1', 'DR-TEST-1', '2026-10-29 12:00:00', 0)"
    )
    conn.close()
