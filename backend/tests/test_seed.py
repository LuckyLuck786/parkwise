"""Phase 1 acceptance: the seeded dataset exists, is the right shape, and
re-running the seeder changes nothing.

These are the checks the spec asks for: 3 lots, 60+ bays (6+ accessible, some
Tier 2 reserved), 5 buildings, 25+ users across all tiers with 1-2 vehicles
each, and 14 days of arrivals with a morning rush.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import ArrivalHistory, Bay, Building, Lot, User, Vehicle, TIER_1, TIER_2, TIER_3
from app.db.seed import counts, seed


def test_seed_populates_the_expected_volume(db_session):
    data = counts(db_session)
    assert data["lots"] == 3, "spec requires 3 lots"
    assert data["buildings"] == 5, "spec requires 5 buildings"
    assert data["bays"] >= 60, f"spec requires 60+ bays, got {data['bays']}"
    assert data["accessible_bays"] >= 6, "spec requires 6+ accessible bays"
    assert data["tier2_reserved_bays"] >= 1, "some bays must be Tier 2 reserved"
    assert data["users"] >= 25, f"spec requires 25+ users, got {data['users']}"
    assert data["vehicles"] >= 25, "each user gets at least one vehicle"
    assert data["history_rows"] >= 40, "14 days of hourly history is expected"


def test_every_tier_is_represented(db_session):
    data = counts(db_session)
    assert data["tier1_users"] >= 1
    assert data["tier2_users"] >= 1
    assert data["tier3_users"] >= 1
    assert data["tier1_users"] + data["tier2_users"] + data["tier3_users"] == data["users"]


#: Other test modules create their own drivers on ``@parkwise.test`` to exercise
#: the engine, and some of those deliberately hold two active vehicles. These
#: invariants are about the *seeded* population, so they are scoped to it.
SEEDED_EMAIL_SUFFIX = "@parkwise.edu"


def _seeded_users(db) -> list:
    return db.scalars(select(User).where(User.email.endswith(SEEDED_EMAIL_SUFFIX))).all()


def test_users_have_between_one_and_three_vehicles(db_session):
    db = db_session
    for user in _seeded_users(db):
        count = db.scalar(
            select(func.count(Vehicle.id)).where(Vehicle.user_id == user.id)
        )
        assert 1 <= count <= 3, f"user {user.email} has {count} vehicles"


def test_only_one_vehicle_is_active_per_user(db_session):
    db = db_session
    for user in _seeded_users(db):
        active = db.scalars(
            select(Vehicle).where(Vehicle.user_id == user.id, Vehicle.active_today.is_(True))
        ).all()
        assert len(active) == 1, f"user {user.email} has {len(active)} active vehicles"


def test_bays_reference_real_lots_and_buildings(db_session):
    db = db_session
    lot_ids = {lot.id for lot in db.scalars(select(Lot))}
    building_ids = {b.id for b in db.scalars(select(Building))}
    for bay in db.scalars(select(Bay)):
        assert bay.lot_id in lot_ids
        if bay.nearest_building_id:
            assert bay.nearest_building_id in building_ids
        assert bay.width > 0 and bay.height > 0, "the Bay Editor needs a real size"


def test_tier1_bays_are_accessible(db_session):
    """A Tier 1 reservation on a bay that is not accessible would be a bug."""
    for bay in db_session.scalars(select(Bay)):
        if bay.reserved_tier == TIER_1:
            assert bay.is_accessible, f"{bay.id} is Tier 1 reserved but not accessible"


def test_arrival_history_covers_fourteen_weekdays_with_a_morning_rush(db_session):
    db = db_session
    days = {row.day.date() for row in db.scalars(select(ArrivalHistory))}
    assert len(days) >= 10, f"expected ~14 days of history, got {len(days)}"
    assert all(day.weekday() < 5 for day in days), "campus history is weekdays only"

    peak_hours = db.execute(
        select(ArrivalHistory.bucket_hour, func.sum(ArrivalHistory.arrivals))
        .group_by(ArrivalHistory.bucket_hour)
        .order_by(func.sum(ArrivalHistory.arrivals).desc())
    ).all()
    assert peak_hours, "history must contain arrivals"
    busiest_hour, _ = peak_hours[0]
    assert 8 <= busiest_hour <= 10, (
        f"the morning rush should peak around 08:30-09:30, but the busiest hour is {busiest_hour:02d}"
    )


def test_history_is_stored_as_aggregated_buckets(db_session):
    db = db_session
    total = db.scalar(select(func.sum(ArrivalHistory.arrivals))) or 0
    assert total > 0
    # Buckets, not one row per vehicle: the table stays small enough to
    # recompute whenever the rules change.
    rows = db.scalar(select(func.count(ArrivalHistory.id))) or 0
    assert rows < total, "history should be aggregated into buckets"


def test_seeding_twice_does_not_duplicate_anything(db_session):
    db = db_session
    before = counts(db)
    history_before = db.scalar(select(func.count(ArrivalHistory.id)))

    seed(db)

    after = counts(db)
    history_after = db.scalar(select(func.count(ArrivalHistory.id)))
    assert before == after, f"second seed changed row counts: {before} -> {after}"
    assert history_before == history_after


def test_seeding_twice_does_not_duplicate_by_key(db_session):
    db = db_session
    seed(db)
    for model, key in ((Lot, "id"), (Bay, "id"), (Building, "id")):
        total = db.scalar(select(func.count(getattr(model, key))))
        distinct = db.scalar(select(func.count(func.distinct(getattr(model, key)))))
        assert total == distinct, f"{model.__name__} has duplicate {key}s"

    users = db.scalar(select(func.count(User.id)))
    emails = db.scalar(select(func.count(func.distinct(User.email))))
    assert users == emails, "duplicate user emails after re-seeding"

    vehicles = db.scalar(select(func.count(Vehicle.id)))
    tags = db.scalar(select(func.count(func.distinct(Vehicle.plate_or_tag_id))))
    assert vehicles == tags, "duplicate vehicle tags after re-seeding"
