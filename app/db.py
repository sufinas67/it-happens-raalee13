import os
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List

DEFAULT_DB_PATH = os.environ.get("FLEETWISE_DB_PATH", "fleetwise.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    registration_no TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('available', 'assigned', 'maintenance', 'inactive')),
    service_due_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    driver_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    availability_status TEXT NOT NULL CHECK (availability_status IN ('available', 'assigned', 'unavailable')),
    max_deliveries_per_shift INTEGER DEFAULT 5,
    deliveries_this_shift INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    pickup TEXT NOT NULL,
    dropoff TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high')),
    load_kg INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'assigned', 'in_transit', 'delivered', 'cancelled')),
    customer_name TEXT NOT NULL,
    otp TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    released_at TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_vehicle ON assignments(vehicle_id) WHERE active=1;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_driver ON assignments(driver_id) WHERE active=1;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_delivery ON assignments(delivery_id) WHERE active=1;
"""

def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("FLEETWISE_DB_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db(db_path: Optional[str] = None):
    conn = get_db_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()

def is_db_empty(db_path: Optional[str] = None) -> bool:
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM vehicles")
        v_count = cur.fetchone()["cnt"]
        cur = conn.execute("SELECT COUNT(*) as cnt FROM drivers")
        dr_count = cur.fetchone()["cnt"]
        cur = conn.execute("SELECT COUNT(*) as cnt FROM deliveries")
        d_count = cur.fetchone()["cnt"]
        return (v_count == 0 and dr_count == 0 and d_count == 0)
    finally:
        conn.close()

def seed_db(db_path: Optional[str] = None, force: bool = False):
    init_db(db_path)
    if not force and not is_db_empty(db_path):
        return

    conn = get_db_connection(db_path)
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if force:
            conn.execute("DELETE FROM assignments")
            conn.execute("DELETE FROM audit_log")
            conn.execute("DELETE FROM idempotency_keys")
            conn.execute("DELETE FROM deliveries")
            conn.execute("DELETE FROM drivers")
            conn.execute("DELETE FROM vehicles")

        # 6 vehicles: mixed types/capacities; one in maintenance, one with overdue service date
        vehicles = [
            ("V-101", "KA-01-EA-1001", "Van", 500, "available", "2026-12-01"),
            ("V-102", "KA-01-EA-1002", "Truck", 2000, "available", "2026-12-15"),
            ("V-103", "KA-01-EA-1003", "Bike", 40, "available", "2026-11-20"),
            ("V-104", "KA-01-EA-1004", "Van", 600, "maintenance", "2026-12-10"),
            ("V-105", "KA-01-EA-1005", "Truck", 1500, "available", "2025-01-10"), # overdue
            ("V-106", "KA-01-EA-1006", "Van", 800, "available", "2026-12-30"),
        ]
        conn.executemany(
            "INSERT INTO vehicles (vehicle_id, registration_no, type, capacity, status, service_due_date) VALUES (?, ?, ?, ?, ?, ?)",
            vehicles
        )

        # 5 drivers: one unavailable, one near its shift limit (4/5)
        drivers = [
            ("DR-001", "Rajesh Kumar", "+91-9876543210", "available", 5, 1),
            ("DR-002", "Priya Sharma", "+91-9876543211", "available", 5, 0),
            ("DR-003", "Amit Patel", "+91-9876543212", "unavailable", 5, 2), # unavailable
            ("DR-004", "Vikram Singh", "+91-9876543213", "available", 5, 4), # near limit (4/5)
            ("DR-005", "Sunita Rao", "+91-9876543214", "available", 5, 2),
        ]
        conn.executemany(
            "INSERT INTO drivers (driver_id, name, phone, availability_status, max_deliveries_per_shift, deliveries_this_shift) VALUES (?, ?, ?, ?, ?, ?)",
            drivers
        )

        # 8 pending deliveries with varied priority, load_kg and customer names
        deliveries = [
            ("D-201", "Warehouse A, Bangalore", "Indiranagar 100ft Rd", "2026-10-30 10:00", "high", 350, "pending", "FreshMart Retail", None, now_str),
            ("D-202", "Warehouse B, Bangalore", "Koramangala 5th Block", "2026-10-30 11:30", "normal", 25, "pending", "TechNova Solutions", None, now_str),
            ("D-203", "Warehouse A, Bangalore", "Whitefield IT Park", "2026-10-30 09:30", "high", 1200, "pending", "Apex Industrial Corp", None, now_str),
            ("D-204", "Hub Central", "HSR Layout Sector 2", "2026-10-30 14:00", "low", 15, "pending", "Sneha Menon", None, now_str),
            ("D-205", "Warehouse C, Electronic City", "Jayanagar 4th Block", "2026-10-30 12:00", "normal", 450, "pending", "GreenBite Organics", None, now_str),
            ("D-206", "Warehouse B, Bangalore", "MG Road Metro Station", "2026-10-30 13:15", "normal", 1800, "pending", "BuildCraft Logistics", None, now_str),
            ("D-207", "Hub Central", "Malleshwaram 8th Cross", "2026-10-30 15:30", "low", 30, "pending", "Ramesh Electronics", None, now_str),
            ("D-208", "Warehouse A, Bangalore", "Hebbal Flyover Depot", "2026-10-30 16:00", "high", 500, "pending", "CloudNine Healthcare", None, now_str),
        ]
        conn.executemany(
            "INSERT INTO deliveries (delivery_id, pickup, dropoff, scheduled_time, priority, load_kg, status, customer_name, otp, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            deliveries
        )

        conn.execute(
            "INSERT INTO audit_log (ts, actor, delivery_id, event, detail) VALUES (?, ?, ?, ?, ?)",
            (now_str, "system", "SYSTEM", "SEEDED", "Initialized database with 6 vehicles, 5 drivers, 8 deliveries")
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def get_db_snapshot(db_path: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db_connection(db_path)
    try:
        tables = ["vehicles", "drivers", "deliveries", "assignments", "audit_log", "idempotency_keys"]
        snapshot = {}
        for table in tables:
            cur = conn.execute(f"SELECT * FROM {table}")
            snapshot[table] = [dict(row) for row in cur.fetchall()]
        return snapshot
    finally:
        conn.close()
