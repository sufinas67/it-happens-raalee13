import csv
import io
import json
import os
import random
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from app.db import get_db_connection, init_db, seed_db, is_db_empty
from app.rules import check_eligibility, is_valid_transition, ACTIVE_DELIVERY_STATUSES

app = FastAPI(title="FleetWise", version="1.0.0")

@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "VALIDATION_ERROR", "message": f"Invalid input parameters: {str(exc)}"}
    )

@app.on_event("startup")
def on_startup():
    init_db()
    if is_db_empty():
        seed_db()

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "FleetWise API running"}

@app.get("/track.html")
def serve_track_html():
    track_path = os.path.join("static", "track.html")
    if os.path.exists(track_path):
        return FileResponse(track_path)
    return {"message": "Tracking page"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ----------------- Helper Functions -----------------

def get_active_assignments(conn) -> tuple[dict, dict]:
    cur = conn.execute("SELECT delivery_id, vehicle_id, driver_id FROM assignments WHERE active = 1")
    rows = cur.fetchall()
    v_map = {r["vehicle_id"]: r["delivery_id"] for r in rows}
    dr_map = {r["driver_id"]: r["delivery_id"] for r in rows}
    return v_map, dr_map

def find_suggested_match(conn, delivery: dict):
    v_active_map, dr_active_map = get_active_assignments(conn)
    cur = conn.execute("SELECT * FROM vehicles WHERE status = 'available' ORDER BY capacity ASC")
    vehicles = [dict(r) for r in cur.fetchall()]

    cur = conn.execute("SELECT * FROM drivers WHERE availability_status = 'available' ORDER BY deliveries_this_shift ASC")
    drivers = [dict(r) for r in cur.fetchall()]

    candidate_vehicle = None
    for v in vehicles:
        ok, _, _ = check_eligibility(
            delivery=delivery,
            vehicle=v,
            active_vehicle_delivery_id=v_active_map.get(v["vehicle_id"]),
            bypass_delivery_status=True
        )
        if ok:
            candidate_vehicle = v
            break

    candidate_driver = None
    for dr in drivers:
        ok, _, _ = check_eligibility(
            delivery=delivery,
            driver=dr,
            active_driver_delivery_id=dr_active_map.get(dr["driver_id"]),
            bypass_delivery_status=True
        )
        if ok:
            candidate_driver = dr
            break

    if candidate_vehicle and candidate_driver:
        why = f"Matched smallest vehicle {candidate_vehicle['vehicle_id']} ({candidate_vehicle['capacity']}kg) and driver {candidate_driver['driver_id']} with lowest shift count ({candidate_driver['deliveries_this_shift']})"
        return candidate_vehicle, candidate_driver, why
    elif not candidate_vehicle and not candidate_driver:
        return None, None, "No eligible vehicle or driver available"
    elif not candidate_vehicle:
        return None, candidate_driver, "No eligible vehicle with sufficient capacity and valid service date"
    else:
        return candidate_vehicle, None, "No eligible driver available under shift limit"

def get_queue_items(conn) -> List[dict]:
    cur = conn.execute("""
        SELECT *, 
        CASE priority 
            WHEN 'high' THEN 1 
            WHEN 'normal' THEN 2 
            WHEN 'low' THEN 3 
            ELSE 4 
        END as priority_order
        FROM deliveries 
        WHERE status = 'pending'
        ORDER BY priority_order ASC, scheduled_time ASC
    """)
    deliveries = [dict(r) for r in cur.fetchall()]
    queue = []
    for d in deliveries:
        d.pop("priority_order", None)
        v, dr, why = find_suggested_match(conn, d)
        queue.append({
            "delivery": d,
            "suggested_vehicle": v,
            "suggested_driver": dr,
            "why": why,
            "eligible": bool(v and dr)
        })
    return queue

def get_next_suggestion(conn) -> Optional[dict]:
    queue = get_queue_items(conn)
    if queue:
        first = queue[0]
        return {
            "delivery_id": first["delivery"]["delivery_id"],
            "priority": first["delivery"]["priority"],
            "load_kg": first["delivery"]["load_kg"],
            "suggested_vehicle_id": first["suggested_vehicle"]["vehicle_id"] if first["suggested_vehicle"] else None,
            "suggested_driver_id": first["suggested_driver"]["driver_id"] if first["suggested_driver"] else None,
            "why": first["why"],
            "eligible": first["eligible"]
        }
    return None

# ----------------- Models -----------------

class VehicleCreate(BaseModel):
    vehicle_id: str = Field(..., min_length=1)
    registration_no: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)
    capacity: int = Field(..., gt=0)
    status: str = Field("available", pattern="^(available|assigned|maintenance|inactive)$")
    service_due_date: str = Field(..., min_length=1)

class VehicleStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(available|assigned|maintenance|inactive)$")

class DriverCreate(BaseModel):
    driver_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    availability_status: str = Field("available", pattern="^(available|assigned|unavailable)$")
    max_deliveries_per_shift: int = Field(5, gt=0)
    deliveries_this_shift: int = Field(0, ge=0)

class DriverStatusUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(available|assigned|unavailable)$")
    availability_status: Optional[str] = Field(None, pattern="^(available|assigned|unavailable)$")

class DeliveryCreate(BaseModel):
    delivery_id: str = Field(..., min_length=1)
    pickup: str = Field(..., min_length=1)
    dropoff: str = Field(..., min_length=1)
    scheduled_time: str = Field(..., min_length=1)
    priority: str = Field("normal", pattern="^(low|normal|high)$")
    load_kg: int = Field(..., gt=0)
    customer_name: str = Field(..., min_length=1)

class AssignRequest(BaseModel):
    vehicle_id: str = Field(..., min_length=1)
    driver_id: str = Field(..., min_length=1)

class StatusUpdate(BaseModel):
    status: str = Field(..., min_length=1)
    otp: Optional[str] = None

# ----------------- CRUD Endpoints -----------------

@app.get("/vehicles")
def get_vehicles():
    conn = get_db_connection()
    try:
        cur = conn.execute("""
            SELECT v.*, a.delivery_id as active_delivery_id 
            FROM vehicles v
            LEFT JOIN assignments a ON v.vehicle_id = a.vehicle_id AND a.active = 1
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

@app.post("/vehicles", status_code=201)
def create_vehicle(v: VehicleCreate):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO vehicles (vehicle_id, registration_no, type, capacity, status, service_due_date) VALUES (?, ?, ?, ?, ?, ?)",
            (v.vehicle_id, v.registration_no, v.type, v.capacity, v.status, v.service_due_date)
        )
        return {"message": "Vehicle created", "vehicle_id": v.vehicle_id}
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            return JSONResponse(status_code=409, content={"error": "DUPLICATE", "message": f"Vehicle ID or registration already exists: {str(e)}"})
        return JSONResponse(status_code=400, content={"error": "VALIDATION_ERROR", "message": str(e)})
    finally:
        conn.close()

@app.patch("/vehicles/{vehicle_id}/status")
def update_vehicle_status(vehicle_id: str, payload: VehicleStatusUpdate):
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM vehicles WHERE vehicle_id = ?", (vehicle_id,))
        vehicle = cur.fetchone()
        if not vehicle:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Vehicle {vehicle_id} not found"})

        new_status = payload.status
        if new_status in ("maintenance", "inactive"):
            cur = conn.execute("SELECT delivery_id FROM assignments WHERE vehicle_id = ? AND active = 1", (vehicle_id,))
            active_assign = cur.fetchone()
            if active_assign:
                return JSONResponse(
                    status_code=409,
                    content={"error": "VEHICLE_UNAVAILABLE", "message": f"Vehicle {vehicle_id} has active assignment for delivery {active_assign['delivery_id']}"}
                )

        conn.execute("UPDATE vehicles SET status = ? WHERE vehicle_id = ?", (new_status, vehicle_id))
        return {"message": f"Vehicle status updated to {new_status}", "vehicle_id": vehicle_id, "status": new_status}
    finally:
        conn.close()

@app.get("/drivers")
def get_drivers():
    conn = get_db_connection()
    try:
        cur = conn.execute("""
            SELECT d.*, a.delivery_id as active_delivery_id 
            FROM drivers d
            LEFT JOIN assignments a ON d.driver_id = a.driver_id AND a.active = 1
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

@app.post("/drivers", status_code=201)
def create_driver(d: DriverCreate):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO drivers (driver_id, name, phone, availability_status, max_deliveries_per_shift, deliveries_this_shift) VALUES (?, ?, ?, ?, ?, ?)",
            (d.driver_id, d.name, d.phone, d.availability_status, d.max_deliveries_per_shift, d.deliveries_this_shift)
        )
        return {"message": "Driver created", "driver_id": d.driver_id}
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            return JSONResponse(status_code=409, content={"error": "DUPLICATE", "message": f"Driver ID already exists: {str(e)}"})
        return JSONResponse(status_code=400, content={"error": "VALIDATION_ERROR", "message": str(e)})
    finally:
        conn.close()

@app.patch("/drivers/{driver_id}/status")
def update_driver_status(driver_id: str, payload: DriverStatusUpdate):
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM drivers WHERE driver_id = ?", (driver_id,))
        driver = cur.fetchone()
        if not driver:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Driver {driver_id} not found"})

        new_status = payload.status or payload.availability_status
        if not new_status:
            return JSONResponse(status_code=400, content={"error": "VALIDATION_ERROR", "message": "Missing status"})

        if new_status == "unavailable":
            cur = conn.execute("SELECT delivery_id FROM assignments WHERE driver_id = ? AND active = 1", (driver_id,))
            active_assign = cur.fetchone()
            if active_assign:
                return JSONResponse(
                    status_code=409,
                    content={"error": "DRIVER_UNAVAILABLE", "message": f"Driver {driver_id} has active assignment for delivery {active_assign['delivery_id']}"}
                )

        conn.execute("UPDATE drivers SET availability_status = ? WHERE driver_id = ?", (new_status, driver_id))
        return {"message": f"Driver status updated to {new_status}", "driver_id": driver_id, "availability_status": new_status}
    finally:
        conn.close()

@app.get("/deliveries")
def get_deliveries(status: Optional[str] = None, q: Optional[str] = None):
    conn = get_db_connection()
    try:
        query = """
            SELECT d.*, a.vehicle_id, a.driver_id, v.type as vehicle_type, dr.name as driver_name
            FROM deliveries d
            LEFT JOIN assignments a ON d.delivery_id = a.delivery_id AND a.active = 1
            LEFT JOIN vehicles v ON a.vehicle_id = v.vehicle_id
            LEFT JOIN drivers dr ON a.driver_id = dr.driver_id
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND d.status = ?"
            params.append(status)
        if q:
            query += " AND (d.delivery_id LIKE ? OR d.customer_name LIKE ? OR d.pickup LIKE ? OR d.dropoff LIKE ?)"
            wildcard = f"%{q}%"
            params.extend([wildcard, wildcard, wildcard, wildcard])

        query += " ORDER BY d.created_at DESC"
        cur = conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

@app.post("/deliveries", status_code=201)
def create_delivery(d: DeliveryCreate):
    conn = get_db_connection()
    try:
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO deliveries (delivery_id, pickup, dropoff, scheduled_time, priority, load_kg, status, customer_name, otp, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?)",
            (d.delivery_id, d.pickup, d.dropoff, d.scheduled_time, d.priority, d.load_kg, d.customer_name, now_str)
        )
        return {"message": "Delivery created", "delivery_id": d.delivery_id, "status": "pending"}
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            return JSONResponse(status_code=409, content={"error": "DUPLICATE", "message": f"Delivery ID already exists: {str(e)}"})
        return JSONResponse(status_code=400, content={"error": "VALIDATION_ERROR", "message": str(e)})
    finally:
        conn.close()

# ----------------- ASSIGNMENT ENDPOINT (ATOMIC) -----------------

@app.post("/deliveries/{delivery_id}/assign", status_code=201)
def assign_delivery(
    delivery_id: str,
    payload: AssignRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    conn = get_db_connection()
    try:
        if idempotency_key:
            cur = conn.execute("SELECT response_json, status_code FROM idempotency_keys WHERE key = ?", (idempotency_key,))
            cached = cur.fetchone()
            if cached:
                return Response(
                    content=cached["response_json"],
                    status_code=cached["status_code"],
                    media_type="application/json"
                )

        conn.execute("BEGIN IMMEDIATE")
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        del_row = cur.fetchone()
        if not del_row:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Delivery {delivery_id} not found"})
        delivery = dict(del_row)

        cur = conn.execute("SELECT * FROM vehicles WHERE vehicle_id = ?", (payload.vehicle_id,))
        vehicle_row = cur.fetchone()
        if not vehicle_row:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Vehicle {payload.vehicle_id} not found"})
        vehicle = dict(vehicle_row)

        cur = conn.execute("SELECT * FROM drivers WHERE driver_id = ?", (payload.driver_id,))
        driver_row = cur.fetchone()
        if not driver_row:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Driver {payload.driver_id} not found"})
        driver = dict(driver_row)

        cur = conn.execute("SELECT delivery_id FROM assignments WHERE vehicle_id = ? AND active = 1", (payload.vehicle_id,))
        v_active = cur.fetchone()
        active_v_del = v_active["delivery_id"] if v_active else None

        cur = conn.execute("SELECT delivery_id FROM assignments WHERE driver_id = ? AND active = 1", (payload.driver_id,))
        dr_active = cur.fetchone()
        active_dr_del = dr_active["delivery_id"] if dr_active else None

        ok, code, reason = check_eligibility(
            delivery=delivery,
            vehicle=vehicle,
            driver=driver,
            active_vehicle_delivery_id=active_v_del,
            active_driver_delivery_id=active_dr_del,
        )

        if not ok:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=409, content={"error": code, "message": reason})

        conn.execute(
            "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES (?, ?, ?, ?, 1)",
            (delivery_id, payload.vehicle_id, payload.driver_id, now_str)
        )
        conn.execute("UPDATE vehicles SET status = 'assigned' WHERE vehicle_id = ?", (payload.vehicle_id,))
        conn.execute("UPDATE drivers SET availability_status = 'assigned' WHERE driver_id = ?", (payload.driver_id,))
        conn.execute("UPDATE deliveries SET status = 'assigned' WHERE delivery_id = ?", (delivery_id,))
        conn.execute(
            "INSERT INTO audit_log (ts, actor, delivery_id, event, detail) VALUES (?, 'dispatcher', ?, 'ASSIGNED', ?)",
            (now_str, delivery_id, f"Assigned vehicle {payload.vehicle_id} and driver {payload.driver_id}")
        )

        res_data = {
            "message": "Delivery successfully assigned",
            "delivery_id": delivery_id,
            "vehicle_id": payload.vehicle_id,
            "driver_id": payload.driver_id,
            "assigned_at": now_str,
            "status": "assigned"
        }
        res_json = json.dumps(res_data)

        if idempotency_key:
            conn.execute(
                "INSERT INTO idempotency_keys (key, response_json, status_code, created_at) VALUES (?, ?, 201, ?)",
                (idempotency_key, res_json, now_str)
            )

        conn.execute("COMMIT")
        return Response(content=res_json, status_code=201, media_type="application/json")

    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        if "UNIQUE constraint failed" in str(e):
            return JSONResponse(status_code=409, content={"error": "DUPLICATE", "message": "Conflict: Resource already assigned in active assignment"})
        return JSONResponse(status_code=500, content={"error": "SERVER_ERROR", "message": str(e)})
    finally:
        conn.close()

# ----------------- STATUS TRANSITION ENDPOINT -----------------

@app.patch("/deliveries/{delivery_id}/status")
def update_delivery_status(delivery_id: str, payload: StatusUpdate):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        del_row = cur.fetchone()
        if not del_row:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Delivery {delivery_id} not found"})

        delivery = dict(del_row)
        current_status = delivery["status"]
        new_status = payload.status

        is_ok, trans_err = is_valid_transition(current_status, new_status, is_assign_endpoint=False)
        if not is_ok:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=409, content={"error": "INVALID_TRANSITION", "message": trans_err})

        released = False

        if new_status == "in_transit":
            otp = f"{random.randint(1000, 9999)}"
            conn.execute("UPDATE deliveries SET status = 'in_transit', otp = ? WHERE delivery_id = ?", (otp, delivery_id))
            conn.execute(
                "INSERT INTO audit_log (ts, actor, delivery_id, event, detail) VALUES (?, 'dispatcher', ?, 'IN_TRANSIT', ?)",
                (now_str, delivery_id, f"Trip started. OTP generated: {otp}")
            )

        elif new_status == "delivered":
            if not payload.otp or str(payload.otp).strip() != str(delivery["otp"]).strip():
                conn.execute("ROLLBACK")
                return JSONResponse(status_code=409, content={"error": "INVALID_OTP", "message": "Invalid OTP provided for delivery confirmation"})

            conn.execute("UPDATE deliveries SET status = 'delivered' WHERE delivery_id = ?", (delivery_id,))
            cur = conn.execute("SELECT * FROM assignments WHERE delivery_id = ? AND active = 1", (delivery_id,))
            assign_row = cur.fetchone()
            if assign_row:
                conn.execute("UPDATE assignments SET active = 0, released_at = ? WHERE assignment_id = ?", (now_str, assign_row["assignment_id"]))
                conn.execute("UPDATE vehicles SET status = 'available' WHERE vehicle_id = ?", (assign_row["vehicle_id"],))
                conn.execute(
                    "UPDATE drivers SET availability_status = 'available', deliveries_this_shift = deliveries_this_shift + 1 WHERE driver_id = ?",
                    (assign_row["driver_id"],)
                )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, delivery_id, event, detail) VALUES (?, 'dispatcher', ?, 'DELIVERED', ?)",
                (now_str, delivery_id, "Delivery completed and resources released")
            )
            released = True

        elif new_status == "cancelled":
            conn.execute("UPDATE deliveries SET status = 'cancelled' WHERE delivery_id = ?", (delivery_id,))
            cur = conn.execute("SELECT * FROM assignments WHERE delivery_id = ? AND active = 1", (delivery_id,))
            assign_row = cur.fetchone()
            if assign_row:
                conn.execute("UPDATE assignments SET active = 0, released_at = ? WHERE assignment_id = ?", (now_str, assign_row["assignment_id"]))
                conn.execute("UPDATE vehicles SET status = 'available' WHERE vehicle_id = ?", (assign_row["vehicle_id"],))
                conn.execute("UPDATE drivers SET availability_status = 'available' WHERE driver_id = ?", (assign_row["driver_id"],))
            conn.execute(
                "INSERT INTO audit_log (ts, actor, delivery_id, event, detail) VALUES (?, 'dispatcher', ?, 'CANCELLED', ?)",
                (now_str, delivery_id, f"Delivery cancelled from {current_status}")
            )
            released = True

        next_sugg = get_next_suggestion(conn) if released else None
        conn.execute("COMMIT")

        res_payload = {
            "message": f"Delivery status updated to {new_status}",
            "delivery_id": delivery_id,
            "status": new_status,
        }
        if released and next_sugg:
            res_payload["next_suggestion"] = next_sugg

        return res_payload

    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return JSONResponse(status_code=500, content={"error": "SERVER_ERROR", "message": str(e)})
    finally:
        conn.close()

# ----------------- ELIGIBILITY & SUGGEST -----------------

@app.get("/deliveries/{delivery_id}/eligibility")
def get_delivery_eligibility(delivery_id: str):
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        del_row = cur.fetchone()
        if not del_row:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Delivery {delivery_id} not found"})

        delivery = dict(del_row)
        v_active_map, dr_active_map = get_active_assignments(conn)

        cur = conn.execute("SELECT * FROM vehicles")
        vehicles = [dict(r) for r in cur.fetchall()]

        cur = conn.execute("SELECT * FROM drivers")
        drivers = [dict(r) for r in cur.fetchall()]

        vehicles_eval = []
        for v in vehicles:
            ok, code, reason = check_eligibility(
                delivery=delivery,
                vehicle=v,
                active_vehicle_delivery_id=v_active_map.get(v["vehicle_id"]),
                bypass_delivery_status=False
            )
            vehicles_eval.append({
                "vehicle_id": v["vehicle_id"],
                "registration_no": v["registration_no"],
                "type": v["type"],
                "capacity": v["capacity"],
                "status": v["status"],
                "eligible": ok,
                "code": code,
                "reason": reason
            })

        drivers_eval = []
        for dr in drivers:
            ok, code, reason = check_eligibility(
                delivery=delivery,
                driver=dr,
                active_driver_delivery_id=dr_active_map.get(dr["driver_id"]),
                bypass_delivery_status=False
            )
            drivers_eval.append({
                "driver_id": dr["driver_id"],
                "name": dr["name"],
                "availability_status": dr["availability_status"],
                "deliveries_this_shift": dr["deliveries_this_shift"],
                "max_deliveries_per_shift": dr["max_deliveries_per_shift"],
                "eligible": ok,
                "code": code,
                "reason": reason
            })

        return {
            "delivery_id": delivery_id,
            "status": delivery["status"],
            "load_kg": delivery["load_kg"],
            "vehicles": vehicles_eval,
            "drivers": drivers_eval
        }
    finally:
        conn.close()

@app.get("/deliveries/{delivery_id}/suggest")
def suggest_delivery_match(delivery_id: str):
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        del_row = cur.fetchone()
        if not del_row:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Delivery {delivery_id} not found"})

        delivery = dict(del_row)
        v, dr, why = find_suggested_match(conn, delivery)
        return {
            "delivery_id": delivery_id,
            "suggested_vehicle": v,
            "suggested_driver": dr,
            "why": why,
            "eligible": bool(v and dr)
        }
    finally:
        conn.close()

# ----------------- QUEUE & STATS -----------------

@app.get("/queue")
def get_queue():
    conn = get_db_connection()
    try:
        return get_queue_items(conn)
    finally:
        conn.close()

@app.get("/stats")
def get_stats():
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT COUNT(*) as total_vehicles FROM vehicles")
        total_v = cur.fetchone()["total_vehicles"]

        cur = conn.execute("SELECT COUNT(*) as avail_v FROM vehicles WHERE status = 'available'")
        avail_v = cur.fetchone()["avail_v"]

        cur = conn.execute("SELECT COUNT(*) as avail_dr FROM drivers WHERE availability_status = 'available'")
        avail_dr = cur.fetchone()["avail_dr"]

        cur = conn.execute("SELECT COUNT(*) as active_del FROM deliveries WHERE status IN ('assigned', 'in_transit')")
        active_del = cur.fetchone()["active_del"]

        cur = conn.execute("SELECT COUNT(*) as deliv_cnt FROM deliveries WHERE status = 'delivered'")
        deliv_cnt = cur.fetchone()["deliv_cnt"]

        cur = conn.execute("SELECT COUNT(*) as pend_cnt FROM deliveries WHERE status = 'pending'")
        pend_cnt = cur.fetchone()["pend_cnt"]

        cur = conn.execute("SELECT COUNT(*) as assigned_v FROM vehicles WHERE status = 'assigned'")
        assigned_v = cur.fetchone()["assigned_v"]
        util_pct = round((assigned_v / total_v * 100), 1) if total_v > 0 else 0.0

        return {
            "available_vehicles": avail_v,
            "available_drivers": avail_dr,
            "active_deliveries": active_del,
            "delivered_count": deliv_cnt,
            "pending_count": pend_cnt,
            "fleet_utilisation_pct": util_pct
        }
    finally:
        conn.close()

# ----------------- BREAKDOWN SWAP -----------------

@app.post("/vehicles/{vehicle_id}/breakdown")
def vehicle_breakdown(vehicle_id: str):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute("SELECT * FROM vehicles WHERE vehicle_id = ?", (vehicle_id,))
        v_row = cur.fetchone()
        if not v_row:
            conn.execute("ROLLBACK")
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Vehicle {vehicle_id} not found"})

        cur = conn.execute("SELECT * FROM assignments WHERE vehicle_id = ? AND active = 1", (vehicle_id,))
        active_assign = cur.fetchone()
        if not active_assign:
            conn.execute("ROLLBACK")
            return JSONResponse(
                status_code=409,
                content={"error": "VEHICLE_UNAVAILABLE", "message": f"Vehicle {vehicle_id} has no active delivery to swap"}
            )

        delivery_id = active_assign["delivery_id"]
        driver_id = active_assign["driver_id"]

        cur = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        delivery = dict(cur.fetchone())

        cur = conn.execute("SELECT * FROM vehicles WHERE vehicle_id != ? AND status = 'available' ORDER BY capacity ASC", (vehicle_id,))
        candidates = [dict(r) for r in cur.fetchall()]

        v_active_map, _ = get_active_assignments(conn)
        replacement_vehicle = None
        rejection_reason = "No available vehicles"

        for cand in candidates:
            ok, code, reason = check_eligibility(
                delivery=delivery,
                vehicle=cand,
                active_vehicle_delivery_id=v_active_map.get(cand["vehicle_id"]),
                bypass_delivery_status=True
            )
            if ok:
                replacement_vehicle = cand
                break
            else:
                rejection_reason = reason

        if not replacement_vehicle:
            conn.execute("ROLLBACK")
            return JSONResponse(
                status_code=409,
                content={"error": "VEHICLE_UNAVAILABLE", "message": f"Cannot swap: No eligible replacement vehicle found ({rejection_reason})"}
            )

        conn.execute("UPDATE vehicles SET status = 'maintenance' WHERE vehicle_id = ?", (vehicle_id,))
        conn.execute("UPDATE assignments SET active = 0, released_at = ? WHERE assignment_id = ?", (now_str, active_assign["assignment_id"]))
        conn.execute(
            "INSERT INTO assignments (delivery_id, vehicle_id, driver_id, assigned_at, active) VALUES (?, ?, ?, ?, 1)",
            (delivery_id, replacement_vehicle["vehicle_id"], driver_id, now_str)
        )
        conn.execute("UPDATE vehicles SET status = 'assigned' WHERE vehicle_id = ?", (replacement_vehicle["vehicle_id"],))

        conn.execute(
            "INSERT INTO audit_log (ts, actor, delivery_id, event, detail) VALUES (?, 'system', ?, 'BREAKDOWN_SWAP', ?)",
            (now_str, delivery_id, f"Vehicle {vehicle_id} broke down (set to maintenance). Swapped to replacement {replacement_vehicle['vehicle_id']}")
        )

        next_sugg = get_next_suggestion(conn)
        conn.execute("COMMIT")

        return {
            "message": f"Breakdown swap successful. Swapped to vehicle {replacement_vehicle['vehicle_id']}",
            "delivery_id": delivery_id,
            "old_vehicle_id": vehicle_id,
            "new_vehicle_id": replacement_vehicle["vehicle_id"],
            "driver_id": driver_id,
            "next_suggestion": next_sugg
        }

    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return JSONResponse(status_code=500, content={"error": "SERVER_ERROR", "message": str(e)})
    finally:
        conn.close()

# ----------------- AUDIT & TRACKING -----------------

@app.get("/deliveries/{delivery_id}/audit")
def get_delivery_audit(delivery_id: str):
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM audit_log WHERE delivery_id = ? ORDER BY id ASC", (delivery_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

@app.get("/track/{delivery_id}")
def get_public_tracking(delivery_id: str):
    conn = get_db_connection()
    try:
        cur = conn.execute("""
            SELECT d.delivery_id, d.pickup, d.dropoff, d.scheduled_time, d.status,
                   v.type as assigned_vehicle_type
            FROM deliveries d
            LEFT JOIN assignments a ON d.delivery_id = a.delivery_id AND a.active = 1
            LEFT JOIN vehicles v ON a.vehicle_id = v.vehicle_id
            WHERE d.delivery_id = ?
        """, (delivery_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "message": f"Delivery {delivery_id} not found"})
        return dict(row)
    finally:
        conn.close()

# ----------------- CSV EXPORT & ADMIN -----------------

@app.get("/export/deliveries.csv")
def export_deliveries_csv():
    conn = get_db_connection()
    try:
        cur = conn.execute("""
            SELECT d.delivery_id, d.customer_name, d.pickup, d.dropoff, d.priority, d.load_kg, d.status, d.scheduled_time,
                   a.vehicle_id, a.driver_id, a.assigned_at
            FROM deliveries d
            LEFT JOIN assignments a ON d.delivery_id = a.delivery_id AND a.active = 1
            ORDER BY d.created_at ASC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        output = io.StringIO()
        fieldnames = ["delivery_id", "customer_name", "pickup", "dropoff", "priority", "load_kg", "status", "scheduled_time", "vehicle_id", "driver_id", "assigned_at"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=deliveries.csv"})
    finally:
        conn.close()

@app.get("/export/audit.csv")
def export_audit_csv():
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM audit_log ORDER BY id ASC")
        rows = [dict(r) for r in cur.fetchall()]
        output = io.StringIO()
        fieldnames = ["id", "ts", "actor", "delivery_id", "event", "detail"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=audit.csv"})
    finally:
        conn.close()

@app.post("/admin/reset")
def admin_reset():
    seed_db(force=True)
    return {"message": "Database reset and seeded with initial demo data"}
