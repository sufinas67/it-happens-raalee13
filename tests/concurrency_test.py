import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import httpx
import uvicorn

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import init_db, seed_db, get_db_connection
from app.main import app

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port

def run_standalone_concurrency_test():
    print("=" * 80)
    print("FLEETWISE - CONCURRENCY ISOLATION & DOUBLE-BOOKING DEFENCE TEST")
    print("=" * 80)

    # Use a designated test db
    test_db_path = "concurrency_standalone.db"
    os.environ["FLEETWISE_DB_PATH"] = test_db_path
    init_db(test_db_path)
    seed_db(test_db_path, force=True)

    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for server ready
    ready = False
    for _ in range(50):
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.1)

    if not ready:
        print("[FAIL] Test server failed to start.")
        sys.exit(1)

    print(f"[INIT] Server online at {base_url}")
    print("[INIT] Seeding 20 concurrent deliveries and 20 distinct drivers...")

    # Seed 20 test deliveries and 20 test drivers
    conn = get_db_connection(test_db_path)
    for i in range(1, 21):
        deliv_id = f"RACE-DELIV-{i:02d}"
        conn.execute(
            "INSERT OR REPLACE INTO deliveries (delivery_id, pickup, dropoff, scheduled_time, priority, load_kg, status, customer_name, created_at) "
            "VALUES (?, 'Hub A', 'Site B', '2026-10-30 10:00', 'high', 100, 'pending', 'Load Corp', '2026-10-29 12:00:00')",
            (deliv_id,)
        )
        dr_id = f"RACE-DR-{i:02d}"
        conn.execute(
            "INSERT OR REPLACE INTO drivers (driver_id, name, phone, availability_status, max_deliveries_per_shift, deliveries_this_shift) "
            "VALUES (?, 'Race Driver', '555-0100', 'available', 5, 0)",
            (dr_id,)
        )
    conn.close()

    target_vehicle = "V-101"
    print(f"[ATTACK] Launching 20 parallel requests contending for the SAME vehicle '{target_vehicle}'...")

    results = []

    def send_assign_request(req_idx):
        deliv_id = f"RACE-DELIV-{req_idx:02d}"
        dr_id = f"RACE-DR-{req_idx:02d}"
        start_t = time.perf_counter()
        status_code = None
        message = ""
        try:
            r = httpx.post(
                f"{base_url}/deliveries/{deliv_id}/assign",
                json={"vehicle_id": target_vehicle, "driver_id": dr_id},
                timeout=10.0
            )
            status_code = r.status_code
            latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
            try:
                body = r.json()
                message = body.get("message", "")
            except Exception:
                message = r.text[:30]
        except Exception as ex:
            status_code = "ERR"
            latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
            message = str(ex)

        return {
            "req_id": req_idx,
            "delivery_id": deliv_id,
            "vehicle_id": target_vehicle,
            "driver_id": dr_id,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "message": message[:45]
        }

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(send_assign_request, i) for i in range(1, 21)]
        results = [f.result() for f in futures]

    server.should_exit = True
    server_thread.join(timeout=2.0)

    # Print Table
    print("\n" + "=" * 90)
    print(f"{'Req #':<6} | {'Delivery':<14} | {'Target Veh':<10} | {'Driver':<10} | {'Status':<7} | {'Time (ms)':<9} | {'Result Message'}")
    print("-" * 90)
    for r in results:
        status_str = f"[{r['status_code']}]"
        print(f"{r['req_id']:<6} | {r['delivery_id']:<14} | {r['vehicle_id']:<10} | {r['driver_id']:<10} | {status_str:<7} | {r['latency_ms']:<9} | {r['message']}")
    print("=" * 90)

    success_count = sum(1 for r in results if r["status_code"] == 201)
    conflict_count = sum(1 for r in results if r["status_code"] == 409)
    other_count = len(results) - (success_count + conflict_count)

    print(f"\n[SUMMARY] Total Requests: {len(results)}")
    print(f"[SUMMARY] Succeeded (201 Created): {success_count} (EXACTLY 1 REQUIRED)")
    print(f"[SUMMARY] Conflicts (409 Rejected): {conflict_count} (EXACTLY 19 REQUIRED)")
    print(f"[SUMMARY] Other/Errors:            {other_count}")

    # Verify DB Integrity
    conn = get_db_connection(test_db_path)
    active_v_assigns = conn.execute("SELECT * FROM assignments WHERE vehicle_id = ? AND active = 1", (target_vehicle,)).fetchall()
    total_active_assigns = conn.execute("SELECT * FROM assignments WHERE active = 1").fetchall()
    conn.close()

    print(f"[VERIFY] Active assignments for vehicle {target_vehicle} in DB: {len(active_v_assigns)}")
    print(f"[VERIFY] Total active assignments created: {len(total_active_assigns)}")

    if os.path.exists(test_db_path):
        try:
            os.remove(test_db_path)
        except Exception:
            pass

    if success_count == 1 and conflict_count == 19 and len(active_v_assigns) == 1:
        print("\n>>> CONCURRENCY TEST PASSED: ZERO DOUBLE-BOOKING OCCURRED <<<\n")
        return 0
    else:
        print("\n>>> CONCURRENCY TEST FAILED <<<\n")
        return 1

if __name__ == "__main__":
    sys.exit(run_standalone_concurrency_test())
