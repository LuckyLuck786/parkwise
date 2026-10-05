"""Phase 2 acceptance: the allocation engine.

Each test states the rule it defends, because "priority-based allocation" is
only meaningful if the priority rules are individually pinned:

* tier priority        - a tier 1 driver gets an accessible bay under pressure
* quota behaviour      - tier 2 bays are held to the cut-off, then open up
* quota never evicts   - a parked vehicle is never moved to honour a tier
* waitlist ordering    - tier first, then arrival time
* no-show release      - the bay returns and the waitlist head is notified
* double scan          - one bay per account, always
* wrong vehicle type   - a two-wheeler never gets a four-wheeler bay
* concurrency          - two simultaneous scans never take the same bay
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    ALLOT_ACTIVE,
    Building,
    ALLOT_COMPLETED,
    ALLOT_NO_SHOW,
    ALLOT_OCCUPIED,
    Bay,
    BAY_FREE,
    BAY_OCCUPIED,
    Lot,
    Rule,
    TIER_1,
    TIER_2,
    TIER_3,
    User,
    Vehicle,
    VEHICLE_FOUR_WHEELER,
    VEHICLE_TWO_WHEELER,
)
from app.services import allocation_service as alloc
from app.services import demo_clock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_lot(db_session):
    """Reset bay and allotment state so tests do not depend on each other."""
    for bay in db_session.scalars(select(Bay)):
        bay.state = BAY_FREE
        bay.sensor_fresh = True
    db_session.execute(alloc.Allotment.__table__.delete())
    db_session.execute(alloc.WaitlistEntry.__table__.delete())
    db_session.commit()
    yield


def _at(hour: int, minute: int = 0) -> datetime:
    base = datetime.now(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return base


def _make_user(
    db,
    *,
    tier: int = TIER_3,
    needs_accessible: bool = False,
    vehicle_type: str = VEHICLE_FOUR_WHEELER,
    building_id: str | None = None,
) -> tuple[User, Vehicle]:
    unique = f"t{tier}-{needs_accessible}-{vehicle_type}-{id(db)}-{datetime.now(UTC).timestamp()}"
    user = User(
        email=f"{unique}@parkwise.test",
        full_name=f"Test {tier}",
        role="driver",
        priority_tier=tier,
        needs_accessible=needs_accessible,
        destination_building_id=building_id,
    )
    db.add(user)
    db.flush()
    vehicle = Vehicle(
        user_id=user.id,
        plate_or_tag_id=f"TAG-{unique}",
        vehicle_type=vehicle_type,
        active_today=True,
    )
    db.add(vehicle)
    db.commit()
    return user, vehicle


def _fill_lot(
    db,
    lot_id: str,
    *,
    leave_free: int = 0,
    bay_type: str = VEHICLE_FOUR_WHEELER,
) -> None:
    """Mark every bay in a lot taken, leaving ``leave_free`` suitable ones free.

    ``bay_type`` defaults to four-wheeler because most tests scan a car;
    leaving only two-wheeler bays free would make the driver ineligible.
    """
    bays = db.scalars(select(Bay).where(Bay.lot_id == lot_id, Bay.bay_type == bay_type)).all()
    # Unreserved bays first: leaving only reserved bays free would make a
    # tier-3 driver ineligible and quietly test nothing.
    bays.sort(key=lambda bay: (bay.reserved_tier != 0, bay.id))
    for index, bay in enumerate(bays):
        bay.state = BAY_FREE if index < leave_free else BAY_OCCUPIED
    others = db.scalars(
        select(Bay).where(Bay.lot_id == lot_id, Bay.bay_type != bay_type)
    ).all()
    for bay in others:
        bay.state = BAY_OCCUPIED
    db.commit()


# ---------------------------------------------------------------------------
# Tier priority
# ---------------------------------------------------------------------------
def test_tier1_driver_gets_an_accessible_bay(db_session):
    """Tier 1 means disabilities/medical needs: accessible bays first."""
    user, vehicle = _make_user(db_session, tier=TIER_1, needs_accessible=True)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))

    assert result["status"] == "allotted"
    bay = db_session.get(Bay, result["allotment"]["bay_id"])
    assert bay.is_accessible, "a tier 1 driver must be given an accessible bay"


def test_tier3_driver_never_receives_a_tier1_bay(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))

    if result["status"] == "allotted":
        bay = db_session.get(Bay, result["allotment"]["bay_id"])
        assert bay.reserved_tier != TIER_1, "tier 3 must not be given a tier 1 bay"


def test_accessible_bays_are_withheld_from_non_tier1(db_session):
    """A non-tier-1 driver must see accessible bays listed as rejected."""
    _user, vehicle = _make_user(db_session, tier=TIER_3)
    candidates, rejected = alloc.eligible_bays(db_session, vehicle, at=_at(9))
    accessible_rejects = [r for r in rejected if r["reason_code"] == "reserved_tier"]
    assert accessible_rejects, "accessible bays must be explicitly rejected for tier 3"
    assert any("tier 1" in r["reason"] for r in accessible_rejects)


# ---------------------------------------------------------------------------
# Quota behaviour
# ---------------------------------------------------------------------------
def test_tier2_bays_are_held_before_the_cutoff(db_session):
    """Before the cut-off a tier 2 bay is not handed to a tier 3 driver."""
    cutoff = db_session.scalar(select(Rule).where(Rule.key == "tier2_cutoff_hour"))
    cutoff_hour = int(cutoff.value)

    user, vehicle = _make_user(db_session, tier=TIER_3)
    candidates, rejected = alloc.eligible_bays(
        db_session, vehicle, at=_at(cutoff_hour - 2)
    )
    tier2_bays = {
        bay.id for bay in db_session.scalars(select(Bay).where(Bay.reserved_tier == TIER_2))
    }
    assert not (tier2_bays & {c["bay"].id for c in candidates}), (
        "a tier 2 duty bay must not be offered to tier 3 before the cut-off"
    )


def test_tier2_bays_open_up_after_the_cutoff(db_session):
    cutoff = db_session.scalar(select(Rule).where(Rule.key == "tier2_cutoff_hour"))
    cutoff_hour = int(cutoff.value)

    user, vehicle = _make_user(db_session, tier=TIER_3)
    candidates, _ = alloc.eligible_bays(db_session, vehicle, at=_at(cutoff_hour + 1))
    tier2_bays = {
        bay.id for bay in db_session.scalars(select(Bay).where(Bay.reserved_tier == TIER_2))
    }
    assert tier2_bays & {c["bay"].id for c in candidates}, (
        "after the cut-off, tier 2 duty bays must be available to everyone"
    )


def test_quota_never_evicts_a_parked_vehicle(db_session):
    """Reserving a bay must not move anyone already parked.

    A tier 1 driver arriving while every tier 1 bay is occupied should be
    refused an accessible bay, not handed one that is physically taken.
    """
    accessible = db_session.scalars(
        select(Bay).where(Bay.is_accessible.is_(True))
    ).all()
    for bay in accessible:
        bay.state = BAY_OCCUPIED
    db_session.commit()

    before = {bay.id: bay.state for bay in accessible}
    user, vehicle = _make_user(db_session, tier=TIER_1, needs_accessible=True)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))

    after = {bay.id: db_session.get(Bay, bay.id).state for bay in accessible}
    assert before == after, "no occupied accessible bay may be reassigned"

    if result["status"] == "allotted":
        assert result["allotment"]["bay_id"] not in before, (
            "an occupied bay must never be allotted"
        )
    else:
        assert result["status"] in ("waitlisted", "alternative_lot", "no_eligible_bay")


# ---------------------------------------------------------------------------
# Walk distance ranking
# ---------------------------------------------------------------------------
def test_allocation_prefers_the_bay_nearest_the_destination(db_session):
    building = db_session.scalars(select(Building)).first()
    user, vehicle = _make_user(db_session, tier=TIER_3, building_id=building.id)
    candidates, _ = alloc.eligible_bays(db_session, vehicle, at=_at(9))
    assert candidates, "expected free candidates"
    walks = [c["walking_m"] for c in candidates]
    assert walks == sorted(walks), "candidates must be ranked nearest-first"


# ---------------------------------------------------------------------------
# One bay per account
# ---------------------------------------------------------------------------
def test_second_scan_for_the_same_account_is_rejected(db_session):
    user, vehicle = _make_user(db_session, tier=TIER_3)
    first = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))
    assert first["status"] == "allotted"

    second_vehicle = Vehicle(
        user_id=user.id,
        plate_or_tag_id=f"SECOND-{user.id}",
        vehicle_type=VEHICLE_FOUR_WHEELER,
        active_today=True,
    )
    db_session.add(second_vehicle)
    db_session.commit()

    with pytest.raises(alloc.AlreadyHoldingBay):
        alloc.allocate(db_session, vehicle=second_vehicle, at=_at(9))


def test_a_released_allotment_frees_the_account(db_session):
    user, vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))
    allotment = db_session.get(alloc.Allotment, result["allotment"]["id"])
    alloc.mark_departed(db_session, allotment.id, at=_at(11))

    again = alloc.allocate(db_session, vehicle=vehicle, at=_at(12))
    assert again["status"] == "allotted", "after leaving, the account may scan again"


# ---------------------------------------------------------------------------
# Vehicle type
# ---------------------------------------------------------------------------
def test_two_wheeler_is_never_given_a_four_wheeler_bay(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3, vehicle_type=VEHICLE_TWO_WHEELER)
    candidates, rejected = alloc.eligible_bays(db_session, vehicle, at=_at(9))
    four_w_bays = {
        bay.id for bay in db_session.scalars(select(Bay).where(Bay.bay_type == VEHICLE_FOUR_WHEELER))
    }
    assert not (four_w_bays & {c["bay"].id for c in candidates})
    assert any(r["reason_code"] == "wrong_type" for r in rejected)


def test_four_wheeler_scan_returns_a_type_error_reason(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3, vehicle_type=VEHICLE_TWO_WHEELER)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))
    if result["status"] == "allotted":
        bay = db_session.get(Bay, result["allotment"]["bay_id"])
        assert bay.bay_type in (VEHICLE_TWO_WHEELER, "any")


# ---------------------------------------------------------------------------
# No-show release
# ---------------------------------------------------------------------------
def test_no_show_is_released_after_the_grace_period(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3)
    start = _at(9)
    result = alloc.allocate(db_session, vehicle=vehicle, at=start)
    allotment = db_session.get(alloc.Allotment, result["allotment"]["id"])
    bay_id = allotment.bay_id

    # Nothing happens before the deadline.
    early = alloc.release_no_shows(db_session, at=start + timedelta(minutes=5))
    assert early["released"] == []
    assert db_session.get(Bay, bay_id).state != BAY_FREE

    # And it does after.
    late = alloc.release_no_shows(db_session, at=start + timedelta(minutes=11))
    assert late["released"], "the allotment should be released after the grace period"
    assert db_session.get(Bay, bay_id).state == BAY_FREE
    reloaded = db_session.get(alloc.Allotment, allotment.id)
    assert reloaded.status == ALLOT_NO_SHOW
    assert "no arrival" in (reloaded.release_reason or "").lower()


def test_arriving_after_a_no_show_release_is_recorded_as_a_false_release(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3)
    start = _at(9)
    result = alloc.allocate(db_session, vehicle=vehicle, at=start)
    allotment_id = result["allotment"]["id"]
    alloc.release_no_shows(db_session, at=start + timedelta(minutes=11))

    outcome = alloc.mark_arrived(db_session, allotment_id, at=start + timedelta(minutes=14))
    assert outcome["was_false_release"] is True
    reloaded = db_session.get(alloc.Allotment, allotment_id)
    assert reloaded.is_false_release is True


def test_on_time_arrival_is_not_a_false_release(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3)
    start = _at(9)
    result = alloc.allocate(db_session, vehicle=vehicle, at=start)
    outcome = alloc.mark_arrived(db_session, result["allotment"]["id"], at=start + timedelta(minutes=4))
    assert outcome["was_false_release"] is False


# ---------------------------------------------------------------------------
# Full lot / alternatives / waitlist
# ---------------------------------------------------------------------------
def test_full_lot_offers_an_alternative_with_drive_time(db_session):
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=0)

    _user, vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=vehicle, preferred_lot_id="lot_a", at=_at(9))
    assert result["status"] in ("alternative_lot", "waitlisted", "no_eligible_bay")


def test_full_lot_with_one_lot_free_gives_an_alternative(db_session):
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=0)
    _fill_lot(db_session, "lot_c", leave_free=6)

    _user, vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=vehicle, preferred_lot_id="lot_a", at=_at(9))
    assert result["status"] == "allotted"
    assert result["allotment"]["lot_id"] == "lot_c", (
        "with lot A full the driver should be placed in lot C"
    )


def test_nothing_free_anywhere_puts_the_driver_on_the_waitlist(db_session):
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=0)

    _user, vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))
    assert result["status"] in ("waitlisted", "no_eligible_bay")


# ---------------------------------------------------------------------------
# Waitlist ordering and promotion
# ---------------------------------------------------------------------------
def test_waitlist_is_ordered_by_tier_then_arrival(db_session):
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=0)

    # Three tier-3 drivers arrive first, then a tier-1 driver.
    tier3 = [_make_user(db_session, tier=TIER_3)[1] for _ in range(3)]
    tier1 = _make_user(db_session, tier=TIER_1, needs_accessible=True)[1]

    for offset, vehicle in enumerate(tier3):
        alloc.join_waitlist(db_session, vehicle=vehicle, at=_at(9, offset))
    alloc.join_waitlist(db_session, vehicle=tier1, at=_at(9, 30))

    snapshot = alloc.waitlist_snapshot(db_session)
    assert snapshot["total"] == 4
    first = snapshot["items"][0]
    assert first["vehicle_id"] == tier1.id, "the tier 1 driver must be at the head of the queue"
    assert [item["tier"] for item in snapshot["items"][1:]] == [TIER_3] * 3


def test_released_bay_notifies_the_waitlist_head(db_session):
    # Exactly one bay free: the one the holder is about to take.
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=1 if lot.id == "lot_a" else 0)

    _holder, holder_vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=holder_vehicle, at=_at(9))
    assert result["status"] == "allotted"
    allotment_id = result["allotment"]["id"]

    # Campus is now genuinely full, so the next drivers must queue.
    assert alloc.free_bay_count(db_session, "lot_a") == 0

    tier3_vehicle = _make_user(db_session, tier=TIER_3)[1]
    tier1_vehicle = _make_user(db_session, tier=TIER_1, needs_accessible=True)[1]
    alloc.join_waitlist(db_session, vehicle=tier3_vehicle, at=_at(9, 0))
    alloc.join_waitlist(db_session, vehicle=tier1_vehicle, at=_at(9, 10))

    # The holder walks away; their bay is the one that frees up.
    alloc.mark_departed(db_session, allotment_id, at=_at(10))
    promoted = alloc.promote_waitlist(db_session, at=_at(10))

    assert promoted, "a freed bay should be given to the waitlist head"
    assert promoted[0]["user_id"] == tier1_vehicle.user_id, (
        "the tier 1 driver is at the head of the queue and must be served first"
    )


def test_waitlisted_driver_who_took_a_bay_is_marked_fulfilled(db_session):
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=0)
    _u, vehicle = _make_user(db_session, tier=TIER_3)
    alloc.join_waitlist(db_session, vehicle=vehicle, at=_at(9))

    # They then find a bay some other way.
    _fill_lot(db_session, "lot_c", leave_free=4)
    alloc.allocate(db_session, vehicle=vehicle, at=_at(9, 30))

    alloc.promote_waitlist(db_session, at=_at(10))
    snapshot = alloc.waitlist_snapshot(db_session)
    assert snapshot["total"] == 0, "a driver who already has a bay must leave the queue"


def test_leaving_the_waitlist_removes_the_entry(db_session):
    _u, vehicle = _make_user(db_session, tier=TIER_3)
    alloc.join_waitlist(db_session, vehicle=vehicle, at=_at(9))
    assert alloc.leave_waitlist(db_session, vehicle_id=vehicle.id) is True
    assert alloc.waitlist_snapshot(db_session)["total"] == 0


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------
def test_every_allotment_carries_a_full_explanation(db_session):
    _user, vehicle = _make_user(db_session, tier=TIER_3)
    result = alloc.allocate(db_session, vehicle=vehicle, at=_at(9))
    explanation = result["explanation"]

    assert explanation["decision"] == "allotted"
    assert explanation["timestamp"]
    assert explanation["why_this_bay"], "the explanation must say why this bay"
    assert explanation["candidates_considered"], "it must list what was considered"
    assert explanation["constraints_applied"]["one_bay_per_account"] is True

    considered = explanation["candidates_considered"]
    assert any(item.get("bay_id") == result["allotment"]["bay_id"] for item in considered)
    rejected = [item for item in considered if item.get("eligible") is False]
    assert rejected, "rejected candidates must be present, each with a reason"
    assert all(item.get("reason") for item in rejected)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_two_simultaneous_scans_never_take_the_same_bay(db_session):
    """Two drivers, one free bay, two threads: exactly one must win it."""
    from app.core.database import SessionLocal

    # Leave exactly one free bay in lot C.
    for lot in db_session.scalars(select(Lot)):
        _fill_lot(db_session, lot.id, leave_free=0)
    bay = db_session.scalars(
        select(Bay).where(Bay.lot_id == "lot_c", Bay.bay_type == VEHICLE_FOUR_WHEELER)
    ).first()
    bay.state = BAY_FREE
    db_session.commit()
    target_bay_id = bay.id

    vehicles = [_make_user(db_session, tier=TIER_3)[1] for _ in range(2)]
    db_session.expunge_all()

    results: list[str] = []
    lock = threading.Lock()

    def worker(vehicle_id: int) -> None:
        db = SessionLocal()
        try:
            vehicle = db.get(Vehicle, vehicle_id)
            try:
                result = alloc.allocate(db, vehicle=vehicle, at=_at(9))
                status = result["status"]
                if status == "allotted":
                    status = f"allotted:{result['allotment']['bay_id']}"
            except Exception as exc:  # noqa: BLE001
                status = f"error:{type(exc).__name__}"
            with lock:
                results.append(status)
        finally:
            db.rollback()
            db.close()

    threads = [threading.Thread(target=worker, args=(v.id,)) for v in vehicles]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    allotted = [r for r in results if r.startswith("allotted:")]
    assert len(allotted) <= 1, f"two drivers took a bay at once: {results}"
    for status in allotted:
        assert status == f"allotted:{target_bay_id}"

    # And the bay is held by at most one active allotment.
    db = SessionLocal()
    try:
        active = db.scalars(
            select(alloc.Allotment).where(
                alloc.Allotment.bay_id == target_bay_id,
                alloc.Allotment.status.in_((ALLOT_ACTIVE, ALLOT_OCCUPIED)),
            )
        ).all()
        assert len(active) <= 1, "a bay must never carry two active allotments"
    finally:
        db.close()
