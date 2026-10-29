from datetime import datetime
from typing import Optional, Dict, Any, Tuple

# Active delivery is strictly defined as assigned OR in_transit
ACTIVE_DELIVERY_STATUSES = ("assigned", "in_transit")

ALLOWED_TRANSITIONS = {
    "pending": {"assigned", "cancelled"},
    "assigned": {"in_transit", "cancelled"},
    "in_transit": {"delivered"},
}

def is_valid_transition(from_status: str, to_status: str, is_assign_endpoint: bool = False) -> Tuple[bool, Optional[str]]:
    """
    Validates allowed status transitions.
    pending->assigned (via assign endpoint only)
    pending->cancelled
    assigned->in_transit
    assigned->cancelled
    in_transit->delivered
    Note: in_transit cannot be cancelled.
    """
    if from_status == "pending" and to_status == "assigned" and not is_assign_endpoint:
        return False, "Transition from pending to assigned is only allowed via the assign endpoint"
    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status in allowed:
        return True, None
    if from_status == "in_transit" and to_status == "cancelled":
        return False, "Deliveries in transit cannot be cancelled"
    return False, f"Invalid status transition from {from_status} to {to_status}"

def check_eligibility(
    delivery: Optional[Dict[str, Any]],
    vehicle: Optional[Dict[str, Any]] = None,
    driver: Optional[Dict[str, Any]] = None,
    active_vehicle_delivery_id: Optional[str] = None,
    active_driver_delivery_id: Optional[str] = None,
    today_str: Optional[str] = None,
    bypass_delivery_status: bool = False,
) -> Tuple[bool, str, str]:
    """
    Single unified eligibility function for FleetWise.
    Used by /assign, /eligibility, /suggest, and breakdown-swap.
    Returns (ok: bool, code: str, reason: str).
    """
    if delivery is None:
        return False, "NOT_FOUND", "Delivery not found"

    if not bypass_delivery_status and delivery.get("status") != "pending":
        return False, "DELIVERY_NOT_PENDING", f"Delivery {delivery.get('delivery_id')} is {delivery.get('status')}, expected pending"

    # Check vehicle eligibility if vehicle provided
    if vehicle is not None:
        v_id = vehicle.get("vehicle_id")
        v_status = vehicle.get("status")
        if v_status != "available":
            return False, "VEHICLE_UNAVAILABLE", f"Vehicle {v_id} is currently {v_status}"
        if active_vehicle_delivery_id:
            return False, "VEHICLE_UNAVAILABLE", f"Vehicle {v_id} is already assigned to active delivery {active_vehicle_delivery_id}"
        
        cap = int(vehicle.get("capacity", 0))
        load = int(delivery.get("load_kg", 0))
        if cap < load:
            return False, "VEHICLE_TOO_SMALL", f"Vehicle {v_id} capacity ({cap}kg) is less than delivery load ({load}kg)"
        
        cur_date = today_str or datetime.utcnow().strftime("%Y-%m-%d")
        service_due = vehicle.get("service_due_date")
        if service_due and service_due < cur_date:
            return False, "VEHICLE_SERVICE_OVERDUE", f"Vehicle {v_id} service was due on {service_due}"

    # Check driver eligibility if driver provided
    if driver is not None:
        dr_id = driver.get("driver_id")
        dr_status = driver.get("availability_status")
        if dr_status != "available":
            return False, "DRIVER_UNAVAILABLE", f"Driver {dr_id} is currently {dr_status}"
        if active_driver_delivery_id:
            return False, "DRIVER_UNAVAILABLE", f"Driver {dr_id} is already assigned to active delivery {active_driver_delivery_id}"
        
        deliveries_count = int(driver.get("deliveries_this_shift", 0))
        max_deliveries = int(driver.get("max_deliveries_per_shift", 5))
        if deliveries_count >= max_deliveries:
            return False, "DRIVER_SHIFT_LIMIT", f"Driver {dr_id} has reached shift limit ({deliveries_count}/{max_deliveries})"

    return True, "OK", "Eligible for assignment"
