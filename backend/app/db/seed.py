"""Idempotent seed.

Running this twice must not duplicate anything: every table is keyed on a
natural id (``lots.id``, ``bays.id``, ``users.email``,
``vehicles.plate_or_tag_id``, the arrival-history unique constraint) and the
seeder checks for existing rows before inserting.

The arrival history is synthetic but not random noise: a morning rush peaking
between 08:30 and 09:30, a smaller lunch bump, and a long tail, with a
per-weekday shape. The predictor is only as honest as this, so it is generated
from an explicit profile rather than ``random.random()``.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import utcnow
from app.db.models import (
    ALLOT_ACTIVE,
    ArrivalHistory,
    AuditLog,
    Bay,
    BAY_FREE,
    Building,
    Device,
    Lot,
    Rule,
    User,
    Vehicle,
    VEHICLE_FOUR_WHEELER,
    VEHICLE_TWO_WHEELER,
    TIER_1,
    TIER_2,
    TIER_3,
)

SEED_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Campus layout
# ---------------------------------------------------------------------------
BUILDINGS = [
    {"id": "bld_engineering", "name": "Engineering Hall", "x": 40, "y": 120, "has_step_free_entrance": True},
    {"id": "bld_library", "name": "Central Library", "x": -60, "y": 30, "has_step_free_entrance": True},
    {"id": "bld_sciences", "name": "Sciences Block", "x": 150, "y": -40, "has_step_free_entrance": False},
    {"id": "bld_union", "name": "Student Union", "x": -20, "y": -90, "has_step_free_entrance": True},
    {"id": "bld_medicine", "name": "Health Sciences", "x": 90, "y": 200, "has_step_free_entrance": True},
]

LOTS = [
    # id, name, x, y, access_road_m, per-type bay counts
    {"id": "lot_a", "name": "Lot A - North Campus", "x": 10, "y": 170, "access_road_m": 90,
     "layout": [("2W", 0, 12), ("4W", 24, 28)]},
    {"id": "lot_b", "name": "Lot B - Library Green", "x": -80, "y": -10, "access_road_m": 140,
     "layout": [("2W", 0, 8), ("4W", 16, 18)]},
    {"id": "lot_c", "name": "Lot C - Overflow Field", "x": 175, "y": -70, "access_road_m": 260,
     "layout": [("2W", 0, 6), ("4W", 12, 10)]},
]

#: Accessible bays, per lot. Tier 1 bays are reserved for tier 1.
ACCESSIBLE_PER_LOT = {"lot_a": 4, "lot_b": 3, "lot_c": 2}
#: Tier 2 duty quota bays, per lot. Held until the cut-off hour, then released.
TIER2_RESERVED_PER_LOT = {"lot_a": 6, "lot_b": 4, "lot_c": 2}

RULES = [
    ("grace_minutes", 10, "Minutes a driver has to arrive before the bay is released."),
    ("tier2_cutoff_hour", 11, "Hour (0-23) after which tier-2 reserved bays open to everyone."),
    ("waitlist_hold_minutes", 10, "Minutes a waitlisted driver has to claim a freed bay."),
    ("tier1_quota_per_lot", 8, "Accessible bays reserved per lot for tier 1."),
    ("tier2_quota_per_lot", 6, "Bays reserved per lot for tier 2 before the cut-off."),
    ("device_offline_seconds", 90, "Missed heartbeats before a bay is marked low-confidence."),
]

# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
FIRST_NAMES = [
    "Ada", "Bo", "Chen", "Dara", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jules",
    "Kai", "Leila", "Mo", "Nora", "Omar", "Priya", "Quinn", "Rosa", "Sam", "Tariq",
    "Uma", "Viktor", "Wen", "Xavi", "Yara", "Zane", "Asha", "Ben", "Cleo", "Dev",
]
LAST_NAMES = [
    "Okafor", "Lindqvist", "Reyes", "Nakamura", "Haddad", "Kowalski", "Mensah",
    "Silva", "Novak", "Farah", "Dubois", "Costa", "Ibrahim", "Zhang", "Moreau",
]

#: (tier, needs_accessible, count) - the spec asks for users across all tiers.
USER_MIX = [
    (TIER_1, True, 5),    # disabilities / medical
    (TIER_2, False, 8),   # faculty + staff on fixed duty
    (TIER_3, False, 14),  # everyone else
]


def _demo_anchor() -> datetime:
    """The most recent weekday at 08:00 UTC - the start of the demo morning."""
    today = utcnow().date()
    while today.weekday() >= 5:  # skip Sat/Sun: campus parking differs
        today -= timedelta(days=1)
    return datetime.combine(today, datetime.min.time(), tzinfo=UTC).replace(hour=8)


# ---------------------------------------------------------------------------
# Arrival profile
# ---------------------------------------------------------------------------
def _arrival_curve(hour: int, weekday: int, lot_index: int, rng: random.Random) -> float:
    """Expected arrivals per hour for one lot on one weekday.

    A morning rush peaking near 09:00, a smaller lunch bump, and an evening
    tail. Weekday shape: Thursday is busiest, weekend-adjacent days quieter.
    """
    peak = 9.0 + 0.3 * lot_index
    morning = math.exp(-((hour - peak) ** 2) / (2 * 1.1**2)) * 34
    lunch = math.exp(-((hour - 12.5) ** 2) / (2 * 0.9**2)) * 11
    evening = math.exp(-((hour - 17.0) ** 2) / (2 * 1.6**2)) * 7
    # Thu busiest, Mon/Fri medium, Tue/Wed quieter.
    weekday_factor = {0: 1.05, 1: 0.92, 2: 0.90, 3: 1.00, 4: 1.12}.get(weekday, 1.0)
    base = (morning + lunch + evening) * weekday_factor
    # A little honest noise so the predictor has something to be uncertain about.
    return max(0.0, base * rng.uniform(0.85, 1.15))


def _seed_arrival_history(db: Session, rng: random.Random) -> int:
    """14 days of historical arrivals, one row per lot/day/hour bucket."""
    anchor = _demo_anchor().date()
    days = [anchor - timedelta(days=offset) for offset in range(1, 15)]
    days = [d for d in days if d.weekday() < 5]  # weekdays only

    rows: list[ArrivalHistory] = []
    for lot_index, lot in enumerate(LOTS):
        lot_capacity_bays = sum(count for _, _, count in lot["layout"])
        for day in days:
            for hour in range(6, 21):
                expected = _arrival_curve(hour, day.weekday(), lot_index, rng)
                arrivals = int(round(rng.gauss(expected, max(0.8, expected * 0.12))))
                arrivals = max(0, min(arrivals, lot_capacity_bays))
                if arrivals == 0:
                    continue
                # Roughly a sixth of arrivals are tier 1 (accessible bays are
                # the scarce resource, so this drives the Tier-1 contention).
                tier1 = max(1, int(round(arrivals * rng.uniform(0.12, 0.20)))) if hour < 15 else 0
                rows.append(
                    ArrivalHistory(
                        lot_id=lot["id"],
                        day=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                        weekday=day.weekday(),
                        bucket_hour=hour,
                        arrivals=arrivals,
                        tier_1_arrivals=min(tier1, arrivals),
                    )
                )
    db.add_all(rows)
    db.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def _seed_buildings(db: Session) -> int:
    existing = {row.id for row in db.scalars(select(Building))}
    added = 0
    for spec in BUILDINGS:
        if spec["id"] in existing:
            continue
        db.add(Building(**spec))
        added += 1
    db.flush()
    return added


def _seed_lots_and_bays(db: Session) -> tuple[int, int]:
    existing_lots = {row.id for row in db.scalars(select(Lot))}
    existing_bays = {row.id for row in db.scalars(select(Bay))}

    added_lots = 0
    added_bays = 0
    for lot_spec in LOTS:
        if lot_spec["id"] not in existing_lots:
            db.add(
                Lot(
                    id=lot_spec["id"],
                    name=lot_spec["name"],
                    x=lot_spec["x"],
                    y=lot_spec["y"],
                    capacity=sum(count for _, _, count in lot_spec["layout"]),
                    access_road_m=lot_spec["access_road_m"],
                )
            )
            added_lots += 1
        db.flush()
        if lot_spec["id"] in existing_lots:
            continue

        building_ids = [b["id"] for b in BUILDINGS]
        accessible_left = ACCESSIBLE_PER_LOT[lot_spec["id"]]
        tier2_left = TIER2_RESERVED_PER_LOT[lot_spec["id"]]

        for bay_type, prefix, count in lot_spec["layout"]:
            is_two_wheeler = bay_type == "2W"
            for index in range(count):
                bay_id = f"{lot_spec['id']}_{'2w' if is_two_wheeler else '4w'}_{index + 1:02d}"
                if bay_id in existing_bays:
                    continue

                # Accessible bays are four-wheeler sized and sit nearest the
                # step-free entrance; tier-2 duty bays sit just inside them.
                is_accessible = accessible_left > 0 and not is_two_wheeler
                reserved_tier = 0
                if is_accessible:
                    accessible_left -= 1
                    reserved_tier = TIER_1
                elif tier2_left > 0 and not is_two_wheeler:
                    tier2_left -= 1
                    reserved_tier = TIER_2

                # Nearest building cycles so walking distance is not degenerate.
                nearest = building_ids[(index + len(building_ids)) % len(building_ids)]
                db.add(
                    Bay(
                        id=bay_id,
                        lot_id=lot_spec["id"],
                        label=("2W" if is_two_wheeler else "4W") + f"-{index + 1:02d}",
                        bay_type=VEHICLE_TWO_WHEELER if is_two_wheeler else VEHICLE_FOUR_WHEELER,
                        is_accessible=is_accessible,
                        reserved_tier=reserved_tier,
                        x=round(index * 2.8, 2) if is_two_wheeler else round(index * 2.8, 2),
                        y=0.0 if is_two_wheeler else -6.0,
                        width=1.2 if is_two_wheeler else 2.5,
                        height=2.4 if is_two_wheeler else 5.0,
                        nearest_building_id=nearest,
                        state=BAY_FREE,
                        device_id=f"dev_{lot_spec['id']}",
                        sensor_fresh=True,
                    )
                )
                added_bays += 1
    db.flush()
    return added_lots, added_bays


def _seed_people(db: Session, rng: random.Random) -> tuple[int, int]:
    existing_users = {row.email for row in db.scalars(select(User))}
    existing_vehicles = {row.plate_or_tag_id for row in db.scalars(select(Vehicle))}

    added_users = 0
    added_vehicles = 0
    counter = 0
    building_ids = [b["id"] for b in BUILDINGS]

    for tier, needs_accessible, count in USER_MIX:
        for _ in range(count):
            counter += 1
            first = FIRST_NAMES[counter % len(FIRST_NAMES)]
            last = LAST_NAMES[(counter * 7) % len(LAST_NAMES)]
            email = f"{first.lower()}.{last.lower()}{counter}@parkwise.edu"
            if email in existing_users:
                continue
            role = "admin" if counter == 1 and tier == TIER_2 else "driver"
            user = User(
                email=email,
                full_name=f"{first} {last}",
                # Demo credentials: password is "parkwise" for every seeded user.
                hashed_password="",
                role=role,
                priority_tier=tier,
                needs_accessible=needs_accessible,
                destination_building_id=building_ids[counter % len(building_ids)],
            )
            db.add(user)
            db.flush()
            added_users += 1

            # 1-3 vehicles per user; tier 1 skews to accessible-friendly 4W.
            vehicle_count = rng.choice([1, 1, 2, 2, 3])
            for v in range(vehicle_count):
                is_two = rng.random() < (0.35 if tier == TIER_1 else 0.45)
                kind = "2W" if is_two else "4W"
                tag = f"PW-{counter:03d}-{kind}-{v + 1}"
                if tag in existing_vehicles:
                    continue
                db.add(
                    Vehicle(
                        user_id=user.id,
                        plate_or_tag_id=tag,
                        vehicle_type=VEHICLE_TWO_WHEELER if is_two else VEHICLE_FOUR_WHEELER,
                        label=f"{first}'s {kind}",
                        # Exactly one vehicle active per user by default.
                        active_today=(v == 0),
                    )
                )
                added_vehicles += 1

    # Enforce "only one vehicle active per account".
    db.flush()
    for user in db.scalars(select(User)):
        vehicles = sorted(db.scalars(select(Vehicle).where(Vehicle.user_id == user.id)), key=lambda v: v.id)
        for index, vehicle in enumerate(vehicles):
            vehicle.active_today = index == 0
    db.flush()
    return added_users, added_vehicles


def _seed_rules(db: Session) -> int:
    existing = {row.key for row in db.scalars(select(Rule))}
    added = 0
    for key, value, description in RULES:
        if key in existing:
            continue
        db.add(Rule(key=key, value=value, description=description))
        added += 1
    db.flush()
    return added


def _seed_devices(db: Session) -> int:
    existing = {row.id for row in db.scalars(select(Device))}
    added = 0
    for lot in LOTS:
        device_id = f"dev_{lot['id']}"
        if device_id in existing:
            continue
        db.add(
            Device(
                id=device_id,
                kind="simulator",
                label=f"{lot['name']} bay array",
                status="online",
                last_heartbeat=utcnow(),
                meta={"lot_id": lot["id"], "protocol": "normalized-events-v1"},
            )
        )
        added += 1
    db.flush()
    return added


def seed(db: Session, *, reset: bool = False, rng_seed: int = 20260101) -> dict[str, int]:
    """Populate the demo dataset. Safe to run repeatedly."""
    rng = random.Random(rng_seed)

    created = {
        "buildings": 0,
        "lots": 0,
        "bays": 0,
        "users": 0,
        "vehicles": 0,
        "rules": 0,
        "devices": 0,
        "history_rows": 0,
    }

    created["buildings"] = _seed_buildings(db)
    lots, bays = _seed_lots_and_bays(db)
    created["lots"] = lots
    created["bays"] = bays
    users, vehicles = _seed_people(db, rng)
    created["users"] = users
    created["vehicles"] = vehicles
    created["rules"] = _seed_rules(db)
    created["devices"] = _seed_devices(db)

    already_seeded = db.scalar(
        select(func.count(ArrivalHistory.id))
    )
    if not already_seeded:
        created["history_rows"] = _seed_arrival_history(db, rng)

    db.add(
        AuditLog(
            actor="seed",
            action="seed_run",
            details={"created": created, "reset": reset, "seed_version": SEED_VERSION},
        )
    )
    db.commit()
    return created


def counts(db: Session) -> dict[str, int]:
    """Row counts for every seeded table - the Phase 1 acceptance check."""
    return {
        "buildings": db.scalar(select(func.count(Building.id))) or 0,
        "lots": db.scalar(select(func.count(Lot.id))) or 0,
        "bays": db.scalar(select(func.count(Bay.id))) or 0,
        "accessible_bays": db.scalar(select(func.count(Bay.id)).where(Bay.is_accessible.is_(True))) or 0,
        "tier2_reserved_bays": db.scalar(select(func.count(Bay.id)).where(Bay.reserved_tier == TIER_2)) or 0,
        "users": db.scalar(select(func.count(User.id))) or 0,
        "tier1_users": db.scalar(select(func.count(User.id)).where(User.priority_tier == TIER_1)) or 0,
        "tier2_users": db.scalar(select(func.count(User.id)).where(User.priority_tier == TIER_2)) or 0,
        "tier3_users": db.scalar(select(func.count(User.id)).where(User.priority_tier == TIER_3)) or 0,
        "vehicles": db.scalar(select(func.count(Vehicle.id))) or 0,
        "rules": db.scalar(select(func.count(Rule.key))) or 0,
        "devices": db.scalar(select(func.count(Device.id))) or 0,
        "history_rows": db.scalar(select(func.count(ArrivalHistory.id))) or 0,
    }
