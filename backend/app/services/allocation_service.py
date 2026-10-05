"""The ParkWise allocation engine.

Product framing
---------------
A gate scan allocates one bay. Everything else - which bay, whether to offer
another lot, whether to waitlist - follows from five rules, in this order:

1. **One bay per account.** A user holding a bay cannot be allotted a second.
   This is a hard rejection, not a queue.
2. **Type and tier first.** A bay must take this vehicle type, and must be
   available to this user's tier. Tier 1 gets accessible bays, Tier 2 gets its
   reserved quota *until the cut-off hour* and then those bays open to everyone.
3. **Walk distance, then fill balance.** Among eligible bays, the one nearest
   the driver's destination building wins; ties break toward the lot with more
   free bays, so one lot does not fill up while another stands empty.
4. **Full lot is an answer, not a failure.** If the preferred lot is full the
   driver is offered the nearest lot with space and an estimated drive time.
   Only when nowhere has space does the driver join the waitlist.
5. **Quotas never evict.** A parked vehicle is never moved to free a reserved
   bay. Reservations *withhold* capacity from general allocation; that is the
   whole difference between a quota and an eviction.

Every path returns an explanation: the rule applied, the candidates considered,
why this bay, and why each rejected bay was rejected.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.geo import drive_minutes, walking_minutes, walking_m
from app.db.models import (
    ALLOT_ACTIVE,
    ALLOT_CANCELLED,
    ALLOT_COMPLETED,
    ALLOT_NO_SHOW,
    ALLOT_OCCUPIED,
    AuditLog,
    Bay,
    BAY_ALLOTTED,
    BAY_BLOCKED,
    BAY_FREE,
    BAY_OCCUPIED,
    Building,
    Lot,
    Rule,
    TIER_1,
    TIER_2,
    TIER_3,
    User,
    VEHICLE_FOUR_WHEELER,
    VEHICLE_TWO_WHEELER,
    Vehicle,
    WaitlistEntry,
    Allotment,
)
from app.services import demo_clock

logger = logging.getLogger("parkwise.allocation")


class AllocationError(Exception):
    """Raised for a refusal the caller should surface as a 4xx."""


class AlreadyHoldingBay(AllocationError):
    def __init__(self, allotment_id: int, bay_id: str) -> None:
        super().__init__(
            f"This account already holds bay {bay_id}. Only one vehicle per "
            f"account can hold a bay at a time."
        )
        self.allotment_id = allotment_id
        self.bay_id = bay_id


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def rule_map(db: Session) -> dict[str, int]:
    """Current tunables. The ``rules`` table is the source of truth so the admin
    UI can edit them without a deploy."""
    rows = db.scalars(select(Rule)).all()
    return {row.key: int(row.value) for row in rows}


def rule(db: Session, key: str, default: int) -> int:
    return rule_map(db).get(key, default)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def _type_matches(bay: Bay, vehicle_type: str) -> bool:
    return bay.bay_type in (vehicle_type, "any")


def _tier_allows(
    bay: Bay,
    *,
    tier: int,
    hour: int,
    cutoff_hour: int,
    quota_key: str,
    reserved_in_use: int,
    quota: int,
) -> tuple[bool, str]:
    """Can this tier use this bay right now?

    Returns ``(allowed, reason)``. ``reserved_in_use`` and ``quota`` let a Tier 2
    reservation be honoured as a *quota* - the bay is withheld from general
    allocation until the cut-off, but never taken away from someone parked.
    """
    if bay.reserved_tier == 0:
        return True, "unreserved bay, open to every tier"

    if bay.reserved_tier == TIER_1:
        if tier == TIER_1:
            return True, "tier 1 accessible bay"
        return False, "reserved for tier 1 (disabilities / medical)"

    if bay.reserved_tier == TIER_2:
        if tier == TIER_2:
            return True, f"tier 2 duty bay, held until the {cutoff_hour:02d}:00 cut-off"
        if hour < cutoff_hour:
            # Held, not merely unused. A reserved bay that could be handed out
            # early would not be a reservation at all - faculty arriving at 09:00
            # for a 10:00 duty would find nothing left.
            return False, f"tier 2 duty bay, held until the {cutoff_hour:02d}:00 cut-off"
        return True, f"tier 2 duty bay, cut-off {cutoff_hour:02d}:00 has passed"

    return True, "unreserved bay, open to every tier"


def _reserve_counts(db: Session, lot_id: str, tier: int) -> int:
    """How many of a lot's tier-2 reserved bays are currently spoken for."""
    bays = db.scalars(
        select(Bay).where(Bay.lot_id == lot_id, Bay.reserved_tier == tier)
    ).all()
    if not bays:
        return 0
    taken = 0
    for bay in bays:
        active = db.scalar(
            select(Allotment).where(
                Allotment.bay_id == bay.id,
                Allotment.status.in_((ALLOT_ACTIVE, ALLOT_OCCUPIED)),
            )
        )
        if active is not None:
            taken += 1
    return taken


def free_bay_count(db: Session, lot_id: str) -> int:
    return db.scalar(
        select(func.count(Bay.id)).where(Bay.lot_id == lot_id, Bay.state == BAY_FREE)
    ) or 0


def eligible_bays(
    db: Session,
    vehicle: Vehicle,
    *,
    at: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split bays into eligible candidates and explicitly rejected ones.

    Returning the rejects is not decoration: the gate kiosk and the explainer
    both need to say *why* the nearest bay was not offered.
    """
    rules = rule_map(db)
    cutoff_hour = rules.get("tier2_cutoff_hour", 11)
    quota = rules.get("tier2_quota_per_lot", 6)

    user = db.get(User, vehicle.user_id)
    tier = user.priority_tier if user else TIER_3
    needs_accessible = bool(user and user.needs_accessible)

    destination = None
    if user and user.destination_building_id:
        destination = db.get(Building, user.destination_building_id)

    lots = {lot.id: lot for lot in db.scalars(select(Lot)).all()}
    buildings = {b.id: b for b in db.scalars(select(Building)).all()}
    reserved_in_use: dict[str, int] = {}

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for bay in db.scalars(select(Bay)).all():
        lot = lots.get(bay.lot_id)
        base = {"bay": bay, "lot": lot}

        if bay.state == BAY_BLOCKED:
            rejected.append({**base, "reason_code": "blocked", "reason": f"{bay.label} is blocked (maintenance)"})
            continue
        if bay.state != BAY_FREE:
            state_reason = "allotted, not yet occupied" if bay.state == BAY_ALLOTTED else "occupied"
            rejected.append({**base, "reason_code": bay.state, "reason": f"{bay.label} is {state_reason}"})
            continue
        if not _type_matches(bay, vehicle.vehicle_type):
            rejected.append(
                {
                    **base,
                    "reason_code": "wrong_type",
                    "reason": f"{bay.label} takes {bay.bay_type.replace('_', ' ')}s",
                }
            )
            continue

        if needs_accessible and bay.is_accessible is False and bay.reserved_tier == TIER_1:
            rejected.append(
                {
                    **base,
                    "reason_code": "not_accessible_reserved",
                    "reason": f"{bay.label} is an accessible bay and this vehicle is not registered as needing one",
                }
            )
            continue

        if bay.reserved_tier == TIER_2 and tier != TIER_2:
            lot_id = bay.lot_id
            if lot_id not in reserved_in_use:
                reserved_in_use[lot_id] = _reserve_counts(db, lot_id, TIER_2)

        allowed, reason = _tier_allows(
            bay,
            tier=tier,
            hour=at.hour,
            cutoff_hour=cutoff_hour,
            quota_key="tier2_quota_per_lot",
            reserved_in_use=reserved_in_use.get(bay.lot_id, 0),
            quota=quota,
        )
        if not allowed:
            rejected.append({**base, "reason_code": "reserved_tier", "reason": f"{bay.label}: {reason}"})
            continue

        building = buildings.get(bay.nearest_building_id) if bay.nearest_building_id else None
        walk = 0.0
        if bay and building and destination is not None:
            walk = walking_m(bay.x, bay.y, destination.x, destination.y)
        elif building is not None:
            walk = walking_m(bay.x, bay.y, building.x, building.y)

        candidates.append(
            {
                "bay": bay,
                "lot": lot,
                "walking_m": walk,
                "walking_min": walking_minutes(walk, lot.walk_speed_mps if lot else 1.35),
                "tier_reason": reason,
                "is_accessible": bay.is_accessible,
            }
        )

    # Rule 3: nearest to the destination building wins; ties break toward the
    # lot with more free bays so one lot does not fill while another stands empty.
    free_by_lot = {lot_id: free_bay_count(db, lot_id) for lot_id in lots}
    candidates.sort(
        key=lambda item: (
            0 if needs_accessible and item["is_accessible"] else 1,
            item["walking_m"],
            -free_by_lot.get(item["lot"].id if item["lot"] else "", 0),
            item["bay"].id,
        )
    )
    return candidates, rejected


# ---------------------------------------------------------------------------
# Allotment
# ---------------------------------------------------------------------------
def active_allotment_for_vehicle(db: Session, vehicle_id: int) -> Allotment | None:
    return db.scalar(
        select(Allotment).where(
            Allotment.vehicle_id == vehicle_id,
            Allotment.status.in_((ALLOT_ACTIVE, ALLOT_OCCUPIED)),
        )
    )


def active_allotment_for_user(db: Session, user_id: int) -> Allotment | None:
    return db.scalar(
        select(Allotment)
        .join(Vehicle, Allotment.vehicle_id == Vehicle.id)
        .where(
            Vehicle.user_id == user_id,
            Allotment.status.in_((ALLOT_ACTIVE, ALLOT_OCCUPIED)),
        )
    )


def _claim_bay(db: Session, bay: Bay) -> bool:
    """Move a bay to ``allotted`` only if it is still free.

    The conditional UPDATE is the concurrency guard: two scans racing for the
    last bay both read it as free, but only one ``WHERE state = 'free'`` can
    match a row, so only one transaction wins. The loser gets ``False`` and
    re-evaluates. SQLite serialises writes anyway; on Postgres this is the
    difference between a double-allotment and a clean rejection.
    """
    result = db.execute(
        select(Bay).where(Bay.id == bay.id, Bay.state == BAY_FREE).with_for_update()
    )
    locked = result.scalar_one_or_none()
    if locked is None:
        return False
    db.execute(
        Bay.__table__.update()
        .where(Bay.id == bay.id, Bay.state == BAY_FREE)
        .values(state=BAY_ALLOTTED)
    )
    db.flush()
    return True


def _alternatives(
    db: Session,
    exclude_lot: str | None,
    vehicle_type: str | None = None,
) -> list[dict[str, Any]]:
    """Lots with free space, nearest first, with a drive-time estimate.

    ``vehicle_type`` matters: offering a four-wheeler driver a lot whose only
    free bays are two-wheeler bays is worse than saying nothing, because they
    will drive over there and find nothing suitable.
    """
    lots = db.scalars(select(Lot)).all()
    rows = []
    for lot in lots:
        if exclude_lot and lot.id == exclude_lot:
            continue
        stmt = select(func.count(Bay.id)).where(Bay.lot_id == lot.id, Bay.state == BAY_FREE)
        if vehicle_type:
            stmt = stmt.where(Bay.bay_type.in_((vehicle_type, "any")))
        free = db.scalar(stmt) or 0
        if free <= 0:
            continue
        rows.append(
            {
                "lot": lot,
                "free_bays": free,
                "drive_minutes": drive_minutes(lot.access_road_m),
            }
        )
    rows.sort(key=lambda row: (row["drive_minutes"], -row["free_bays"], row["lot"].id))
    return rows


def allocate(
    db: Session,
    *,
    vehicle: Vehicle,
    preferred_lot_id: str | None = None,
    at: datetime | None = None,
    actor: str = "gate",
) -> dict[str, Any]:
    """Allocate a bay for a scanned vehicle, or explain why we cannot.

    Returns a dict rather than raising for the ordinary "no space" case, because
    a full lot is a legitimate answer the driver needs explained. Raises only for
    genuine misuse (an account already holding a bay).
    """
    at = at or demo_clock.now(db)

    # Rule 1: one bay per account.
    holding = active_allotment_for_user(db, vehicle.user_id)
    if holding is not None:
        raise AlreadyHoldingBay(holding.id, holding.bay_id)

    candidates, rejected = eligible_bays(db, vehicle, at=at)

    preferred = [c for c in candidates if preferred_lot_id and c["lot"] and c["lot"].id == preferred_lot_id]
    elsewhere = [c for c in candidates if not preferred_lot_id or not c["lot"] or c["lot"].id != preferred_lot_id]
    ordered = preferred + elsewhere

    considered = [
        {
            "bay_id": c["bay"].id,
            "bay_label": c["bay"].label,
            "lot_id": c["lot"].id if c["lot"] else None,
            "walking_m": round(c["walking_m"], 1),
            "eligible": True,
        }
        for c in ordered
    ]
    considered.extend(
        {
            "bay_id": r["bay"].id,
            "bay_label": r["bay"].label,
            "lot_id": r["lot"].id if r["lot"] else None,
            "eligible": False,
            "reason_code": r["reason_code"],
            "reason": r["reason"],
        }
        for r in rejected
    )

    for candidate in ordered:
        bay = candidate["bay"]
        if not _claim_bay(db, bay):
            # Lost a race for this bay. Record the miss and try the next one.
            rejected.append(
                {
                    "bay": bay,
                    "lot": candidate["lot"],
                    "reason_code": "raced",
                    "reason": f"{bay.label} was claimed by another driver a moment ago",
                }
            )
            continue

        grace = rule(db, "grace_minutes", 10)
        allotment = Allotment(
            vehicle_id=vehicle.id,
            bay_id=bay.id,
            lot_id=bay.lot_id,
            status=ALLOT_ACTIVE,
            allotted_at=at,
            hold_expires_at=at + timedelta(minutes=grace),
            walking_distance_m=candidate["walking_m"],
            explanation={
                "decision": "allotted",
                "rule": "type_and_tier_then_walk_distance",
                "timestamp": at.isoformat(),
                "why_this_bay": [
                    f"{bay.label} is {candidate['tier_reason']}",
                    f"takes {bay.bay_type.replace('_', ' ')}s, which matches this vehicle",
                    (
                        f"{round(candidate['walking_m'])} m walk to the destination "
                        f"building ({candidate['walking_min']} min)"
                        if candidate["walking_m"]
                        else "no destination building set, so walk distance did not rank it"
                    ),
                ],
                "candidates_considered": considered,
                "constraints_applied": {
                    "one_bay_per_account": True,
                    "vehicle_type": vehicle.vehicle_type,
                    "grace_minutes": grace,
                    "hold_expires_at": (at + timedelta(minutes=grace)).isoformat(),
                },
            },
        )
        db.add(allotment)
        db.flush()
        db.add(
            AuditLog(
                actor=actor,
                action="allot_bay",
                entity="allotment",
                entity_id=str(allotment.id),
                details={"bay_id": bay.id, "lot_id": bay.lot_id, "vehicle_id": vehicle.id},
            )
        )
        db.commit()
        return {
            "status": "allotted",
            "allotment": allotment.to_dict(),
            "message": (
                f"Bay {bay.label} is yours. Arrive within {grace} minutes or it "
                f"returns to the pool."
            ),
            "explanation": allotment.explanation,
        }

    # No bay anywhere. Rule 4: offer another lot, then the waitlist.
    alternatives = _alternatives(db, preferred_lot_id, vehicle.vehicle_type)
    if not alternatives and candidates == [] and rejected:
        # Distinguish "everywhere full" from "nothing suits this vehicle".
        every_lot_full = all(free_bay_count(db, lot.id) == 0 for lot in db.scalars(select(Lot)))
        if not every_lot_full:
            return {
                "status": "no_eligible_bay",
                "message": (
                    "There is space on campus, but nothing suitable for this "
                    "vehicle right now."
                ),
                "candidates_considered": considered,
                "alternatives": [],
            }

    if alternatives:
        best = alternatives[0]
        return {
            "status": "alternative_lot",
            "message": (
                f"{preferred_lot_id and 'The requested lot is full. ' or ''}"
                f"{best['lot'].name} has {best['free_bays']} bay(s) free, "
                f"about {best['drive_minutes']} min drive."
            ),
            "alternatives": [
                {
                    "lot_id": row["lot"].id,
                    "lot_name": row["lot"].name,
                    "free_bays": row["free_bays"],
                    "drive_minutes": row["drive_minutes"],
                }
                for row in alternatives
            ],
            "candidates_considered": considered,
        }

    return {
        "status": "waitlisted",
        "message": "Every bay on campus is taken. You are on the waitlist.",
        "candidates_considered": considered,
        "alternatives": [],
    }


# ---------------------------------------------------------------------------
# Arrival, no-show release, completion
# ---------------------------------------------------------------------------
def mark_arrived(db: Session, allotment_id: int, *, at: datetime | None = None) -> dict[str, Any]:
    at = at or demo_clock.now(db)
    allotment = db.get(Allotment, allotment_id)
    if allotment is None:
        raise LookupError(f"Allotment {allotment_id} not found")
    if allotment.status == ALLOT_COMPLETED:
        raise ValueError("That allotment is already completed.")

    was_no_show_release = allotment.status == ALLOT_NO_SHOW
    allotment.status = ALLOT_OCCUPIED
    allotment.arrived_at = at
    allotment.hold_expires_at = None

    bay = db.get(Bay, allotment.bay_id)
    if bay is not None:
        bay.state = BAY_OCCUPIED
        bay.occupied_since = at

    # A driver who turns up after we released the bay makes the release wrong.
    # That is recorded rather than quietly forgiven.
    if was_no_show_release:
        allotment.is_false_release = True
        allotment.release_reason = (
            (allotment.release_reason or "") + " | driver arrived after release"
        ).strip(" |")

    db.add(
        AuditLog(
            actor="sensor",
            action="vehicle_arrived",
            entity="allotment",
            entity_id=str(allotment.id),
            details={"bay_id": allotment.bay_id, "false_release": was_no_show_release},
        )
    )
    db.commit()
    return {
        "allotment": allotment.to_dict(),
        "was_false_release": was_no_show_release,
    }


def mark_departed(db: Session, allotment_id: int, *, at: datetime | None = None) -> dict[str, Any]:
    at = at or demo_clock.now(db)
    allotment = db.get(Allotment, allotment_id)
    if allotment is None:
        raise LookupError(f"Allotment {allotment_id} not found")
    allotment.status = ALLOT_COMPLETED
    allotment.released_at = at
    bay = db.get(Bay, allotment.bay_id)
    if bay is not None and bay.state in (BAY_OCCUPIED, BAY_ALLOTTED):
        bay.state = BAY_FREE
        bay.occupied_since = None
    db.commit()
    return allotment.to_dict()


def release_no_shows(db: Session, *, at: datetime | None = None) -> dict[str, Any]:
    """Return un-arrived allotments to the pool once grace has passed.

    Then promote the head of the waitlist into the freed bay. Notification is a
    separate concern: this function returns who should be told and the
    notification service decides how.
    """
    at = at or demo_clock.now(db)
    released: list[dict[str, Any]] = []

    stale = db.scalars(
        select(Allotment).where(
            Allotment.status == ALLOT_ACTIVE,
            Allotment.hold_expires_at.is_not(None),
            Allotment.hold_expires_at <= at,
        )
    ).all()

    for allotment in stale:
        mins_late = (at - allotment.hold_expires_at).total_seconds() / 60.0
        allotment.status = ALLOT_NO_SHOW
        allotment.released_at = at
        allotment.release_reason = (
            f"no arrival within the grace period "
            f"({round(mins_late)} min past the deadline)"
        )
        bay = db.get(Bay, allotment.bay_id)
        if bay is not None and bay.state == BAY_ALLOTTED:
            bay.state = BAY_FREE
        released.append(
            {
                "allotment_id": allotment.id,
                "bay_id": allotment.bay_id,
                "lot_id": allotment.lot_id,
                "vehicle_id": allotment.vehicle_id,
                "reason": allotment.release_reason,
            }
        )
        db.add(
            AuditLog(
                actor="system",
                action="release_no_show",
                entity="allotment",
                entity_id=str(allotment.id),
                details={"bay_id": allotment.bay_id, "minutes_late": round(mins_late, 1)},
            )
        )

    promoted = promote_waitlist(db, at=at) if released else []

    db.commit()
    return {
        "released": released,
        "waitlist_promoted": promoted,
        "timestamp": at.isoformat(),
    }


def cancel_allotment(db: Session, allotment_id: int, *, reason: str, at: datetime | None = None) -> Allotment:
    at = at or demo_clock.now(db)
    allotment = db.get(Allotment, allotment_id)
    if allotment is None:
        raise LookupError(f"Allotment {allotment_id} not found")
    allotment.status = ALLOT_CANCELLED
    allotment.released_at = at
    allotment.release_reason = reason
    bay = db.get(Bay, allotment.bay_id)
    if bay is not None and bay.state in (BAY_ALLOTTED, BAY_OCCUPIED):
        bay.state = BAY_FREE
    db.commit()
    return allotment


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------
def join_waitlist(
    db: Session,
    *,
    vehicle: Vehicle,
    lot_pref: str | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Add a vehicle to the waitlist, or return the entry it already has.

    Ordering is by tier then arrival time (rule 5). A driver who is already
    waiting is not queued twice.
    """
    at = at or demo_clock.now(db)

    existing = db.scalar(
        select(WaitlistEntry).where(
            WaitlistEntry.vehicle_id == vehicle.id,
            WaitlistEntry.status == "waiting",
        )
    )
    if existing is not None:
        return {"entry": existing.to_dict(position=waitlist_position(db, existing)), "created": False}

    entry = WaitlistEntry(
        vehicle_id=vehicle.id,
        lot_pref=lot_pref,
        created_at=at,
        status="waiting",
    )
    db.add(entry)
    db.flush()
    db.add(
        AuditLog(
            actor="gate",
            action="waitlist_join",
            entity="waitlist",
            entity_id=str(entry.id),
            details={"vehicle_id": vehicle.id, "lot_pref": lot_pref},
        )
    )
    db.commit()
    return {"entry": entry.to_dict(position=waitlist_position(db, entry)), "created": True}


def _waitlist_order(db: Session, lot_pref: str | None = None):
    """Waiting entries ordered by tier first, then by arrival time."""
    entries = db.scalars(
        select(WaitlistEntry).where(WaitlistEntry.status == "waiting")
    ).all()
    tiers = {}
    for entry in entries:
        vehicle = db.get(Vehicle, entry.vehicle_id)
        user = db.get(User, vehicle.user_id) if vehicle else None
        tiers[entry.id] = user.priority_tier if user else TIER_3
    filtered = [
        e for e in entries if lot_pref is None or e.lot_pref in (None, lot_pref)
    ] or entries
    return sorted(filtered, key=lambda e: (tiers[e.id], e.created_at, e.id))


def waitlist_position(db: Session, entry: WaitlistEntry) -> int:
    ordered = _waitlist_order(db)
    for index, item in enumerate(ordered, start=1):
        if item.id == entry.id:
            return index
    return 0


def waitlist_snapshot(db: Session) -> dict[str, Any]:
    ordered = _waitlist_order(db)
    rows = []
    for index, entry in enumerate(ordered, start=1):
        vehicle = db.get(Vehicle, entry.vehicle_id)
        user = db.get(User, vehicle.user_id) if vehicle else None
        data = entry.to_dict(position=index)
        data["tier"] = user.priority_tier if user else TIER_3
        data["needs_accessible"] = bool(user and user.needs_accessible)
        rows.append(data)
    return {"total": len(rows), "items": rows, "order": "priority tier, then arrival time"}


def leave_waitlist(db: Session, *, vehicle_id: int) -> bool:
    entry = db.scalar(
        select(WaitlistEntry).where(
            WaitlistEntry.vehicle_id == vehicle_id,
            WaitlistEntry.status == "waiting",
        )
    )
    if entry is None:
        return False
    entry.status = "cancelled"
    db.commit()
    return True


def promote_waitlist(db: Session, *, at: datetime | None = None) -> list[dict[str, Any]]:
    """Fill freed bays from the waitlist, head first.

    The bay is *held* for the waitlisted driver rather than dropped straight
    back into the free pool, so the promise made in the notification is real.
    """
    at = at or demo_clock.now(db)
    hold_minutes = rule(db, "waitlist_hold_minutes", 10)
    promoted: list[dict[str, Any]] = []

    free = db.scalars(select(Bay).where(Bay.state == BAY_FREE)).all()
    if not free:
        return promoted

    for entry in _waitlist_order(db):
        vehicle = db.get(Vehicle, entry.vehicle_id)
        if vehicle is None:
            entry.status = "expired"
            continue
        # A driver who took a bay some other way is no longer waiting.
        if active_allotment_for_vehicle(db, vehicle.id) is not None:
            entry.status = "fulfilled"
            entry.fulfilled_at = at
            continue

        candidates, _ = eligible_bays(db, vehicle, at=at)
        if not candidates:
            continue
        bay = candidates[0]["bay"]
        if not _claim_bay(db, bay):
            continue

        allotment = Allotment(
            vehicle_id=vehicle.id,
            bay_id=bay.id,
            lot_id=bay.lot_id,
            status=ALLOT_ACTIVE,
            allotted_at=at,
            hold_expires_at=at + timedelta(minutes=hold_minutes),
            walking_distance_m=candidates[0]["walking_m"],
            explanation={
                "decision": "waitlist_promoted",
                "rule": "waitlist_order_tier_then_arrival",
                "timestamp": at.isoformat(),
                "why_this_bay": [
                    f"You were next on the waitlist (tier {db.get(User, vehicle.user_id).priority_tier if db.get(User, vehicle.user_id) else '?'})",
                    f"Bay {bay.label} freed up",
                    f"held for you for {hold_minutes} minutes",
                ],
                "constraints_applied": {"waitlist_hold_minutes": hold_minutes},
            },
        )
        db.add(allotment)
        entry.status = "notified"
        entry.notified_at = at
        db.flush()
        promoted.append(
            {
                "allotment_id": allotment.id,
                "bay_id": bay.id,
                "lot_id": bay.lot_id,
                "vehicle_id": vehicle.id,
                "user_id": vehicle.user_id,
                "hold_expires_at": allotment.hold_expires_at.isoformat(),
                "message": (
                    f"A bay opened up at {bay.lot_id}. You have {hold_minutes} "
                    f"minutes to claim {bay.label}."
                ),
            }
        )
        db.add(
            AuditLog(
                actor="system",
                action="waitlist_promote",
                entity="allotment",
                entity_id=str(allotment.id),
                details={"bay_id": bay.id, "vehicle_id": vehicle.id},
            )
        )

    db.commit()
    return promoted
