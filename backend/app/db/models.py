"""ParkWise data model.

Design notes that are worth stating once, here, rather than in every service:

* **Tiers are quotas, never eviction.** A reserved bay is *withheld* from
  general allocation while it is held; a vehicle already parked is never moved
  or towed to honour a tier.
* **Occupancy is a belief, not a fact.** ``bays.state`` is what the system
  believes; ``bay_events`` is the evidence trail behind it and
  ``reconcile_service`` flags disagreements. Nothing auto-punishes.
* **No plate images anywhere.** ``vehicles.plate_or_tag_id`` is masked for every
  non-admin view, and the vehicle's identity at the gate is a tag id, not a
  number plate photograph.
"""

from __future__ import annotations

from datetime import UTC, datetime, time as dt_time
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UTCDateTime, utcnow

# --- enumerations as plain strings (SQLite has no native enum) -------------
ROLE_DRIVER = "driver"
ROLE_ADMIN = "admin"
ROLE_GATE_OPERATOR = "gate_operator"

VEHICLE_TWO_WHEELER = "two_wheeler"
VEHICLE_FOUR_WHEELER = "four_wheeler"

TIER_1 = 1  # disabilities / medical needs
TIER_2 = 2  # faculty, staff on fixed duty, service & emergency
TIER_3 = 3  # everyone else

BAY_FREE = "free"
BAY_ALLOTTED = "allotted"
BAY_OCCUPIED = "occupied"
BAY_BLOCKED = "blocked"
BAY_UNKNOWN = "unknown"

ALLOT_ACTIVE = "allotted"
ALLOT_OCCUPIED = "occupied"
ALLOT_COMPLETED = "completed"
ALLOT_NO_SHOW = "no_show_released"
ALLOT_CANCELLED = "cancelled"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def mask_tag(value: str | None) -> str | None:
    """Mask a plate or tag id for non-admin views.

    Keeps enough to recognise a vehicle the user already told us about without
    exposing the identifier to anyone browsing the lot.
    """
    if not value:
        return value
    text_value = value.strip()
    if len(text_value) <= 4:
        return "*" * len(text_value)
    return f"{'*' * (len(text_value) - 4)}{text_value[-4:]}"


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Rule(Base):
    """Tunables the admin UI edits. One seeded row per key."""

    __tablename__ = "rules"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "description": self.description,
            "updated_at": _iso(self.updated_at),
        }


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    x: Mapped[float] = mapped_column(default=0.0)
    y: Mapped[float] = mapped_column(default=0.0)
    has_step_free_entrance: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    bays: Mapped[list["Bay"]] = relationship(back_populates="nearest_building")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "has_step_free_entrance": self.has_step_free_entrance,
            "notes": self.notes,
        }


class Lot(Base):
    __tablename__ = "lots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    x: Mapped[float] = mapped_column(default=0.0)
    y: Mapped[float] = mapped_column(default=0.0)
    capacity: Mapped[int] = mapped_column(Integer, default=0)
    walk_speed_mps: Mapped[float] = mapped_column(default=1.35)
    access_road_m: Mapped[float] = mapped_column(
        default=120.0, doc="Road distance from the gate, used for drive-time estimates."
    )

    bays: Mapped[list["Bay"]] = relationship(back_populates="lot")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "capacity": self.capacity,
            "walk_speed_mps": self.walk_speed_mps,
            "access_road_m": self.access_road_m,
        }


class Bay(Base):
    __tablename__ = "bays"
    __table_args__ = (
        Index("ix_bays_lot_state", "lot_id", "state"),
        Index("ix_bays_type", "bay_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(32))
    #: two_wheeler / four_wheeler / any
    bay_type: Mapped[str] = mapped_column(String(16), default="any")
    is_accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 0 = open to everyone, 1 = Tier 1 only, 2 = Tier 2 quota, 3 = unreserved.
    reserved_tier: Mapped[int] = mapped_column(Integer, default=0)
    #: Layout position, mirrored by the Bay Editor and the thermocol model.
    x: Mapped[float] = mapped_column(default=0.0)
    y: Mapped[float] = mapped_column(default=0.0)
    width: Mapped[float] = mapped_column(default=2.5)
    height: Mapped[float] = mapped_column(default=5.0)
    nearest_building_id: Mapped[str | None] = mapped_column(
        ForeignKey("buildings.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), default=BAY_FREE)
    #: Set when a sensor backs this bay; drives the low-confidence badge.
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: False once a device has missed heartbeats - state is then scan-inferred.
    sensor_fresh: Mapped[bool] = mapped_column(Boolean, default=True)
    occupied_since: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    lot: Mapped[Lot] = relationship(back_populates="bays")
    nearest_building: Mapped[Building | None] = relationship(back_populates="bays")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lot_id": self.lot_id,
            "label": self.label,
            "bay_type": self.bay_type,
            "is_accessible": self.is_accessible,
            "reserved_tier": self.reserved_tier,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "nearest_building_id": self.nearest_building_id,
            "state": self.state,
            "device_id": self.device_id,
            "sensor_fresh": self.sensor_fresh,
            "occupied_since": _iso(self.occupied_since),
            "low_confidence": not self.sensor_fresh,
        }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(24), default=ROLE_DRIVER)
    priority_tier: Mapped[int] = mapped_column(Integer, default=TIER_3)
    needs_accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    destination_building_id: Mapped[str | None] = mapped_column(
        ForeignKey("buildings.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    vehicles: Mapped[list["Vehicle"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def to_dict(self, *, mask: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "full_name": self.full_name,
            "role": self.role,
            "priority_tier": self.priority_tier,
            "needs_accessible": self.needs_accessible,
            "destination_building_id": self.destination_building_id,
        }


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plate_or_tag_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    vehicle_type: Mapped[str] = mapped_column(String(16), default=VEHICLE_FOUR_WHEELER)
    label: Mapped[str] = mapped_column(String(80), default="")
    active_today: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="vehicles")

    def to_dict(self, *, mask: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "plate_or_tag_id": mask_tag(self.plate_or_tag_id) if mask else self.plate_or_tag_id,
            "vehicle_type": self.vehicle_type,
            "label": self.label,
            "active_today": self.active_today,
        }


class Allotment(Base):
    __tablename__ = "allotments"
    __table_args__ = (
        Index("ix_allotments_status", "status"),
        Index("ix_allotments_vehicle", "vehicle_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    bay_id: Mapped[str] = mapped_column(ForeignKey("bays.id", ondelete="CASCADE"))
    lot_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default=ALLOT_ACTIVE)
    allotted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    #: Bay is held but the driver has not arrived yet.
    hold_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_false_release: Mapped[bool] = mapped_column(Boolean, default=False)
    walking_distance_m: Mapped[float] = mapped_column(default=0.0)
    #: The full explanation object. Never reconstructed by the client.
    explanation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    vehicle: Mapped[Vehicle] = relationship()
    bay: Mapped[Bay] = relationship()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "vehicle_id": self.vehicle_id,
            "bay_id": self.bay_id,
            "lot_id": self.lot_id,
            "status": self.status,
            "allotted_at": _iso(self.allotted_at),
            "hold_expires_at": _iso(self.hold_expires_at),
            "arrived_at": _iso(self.arrived_at),
            "released_at": _iso(self.released_at),
            "release_reason": self.release_reason,
            "is_false_release": self.is_false_release,
            "walking_distance_m": round(self.walking_distance_m, 1),
            "explanation": self.explanation,
        }


class WaitlistEntry(Base):
    __tablename__ = "waitlist"
    __table_args__ = (Index("ix_waitlist_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    lot_pref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    vehicle: Mapped[Vehicle] = relationship()

    def to_dict(self, *, position: int | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "vehicle_id": self.vehicle_id,
            "lot_pref": self.lot_pref,
            "created_at": _iso(self.created_at),
            "status": self.status,
            "notified_at": _iso(self.notified_at),
            "fulfilled_at": _iso(self.fulfilled_at),
            "position": position,
        }


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_ts", "user_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(24), default="in_app")
    #: False for the SMS/Telegram stubs, which log instead of sending.
    delivered: Mapped[bool] = mapped_column(Boolean, default=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    read: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "message": self.message,
            "channel": self.channel,
            "delivered": self.delivered,
            "simulated": self.channel != "in_app",
            "ts": _iso(self.ts),
            "read": self.read,
        }


class ScanEvent(Base):
    __tablename__ = "scan_events"
    __table_args__ = (Index("ix_scans_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    tag_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8), default="in")
    source: Mapped[str] = mapped_column(String(24), default="simulator")
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    outcome: Mapped[str] = mapped_column(String(32), default="accepted")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "vehicle_id": self.vehicle_id,
            "tag_id": mask_tag(self.tag_id),
            "direction": self.direction,
            "source": self.source,
            "ts": _iso(self.ts),
            "outcome": self.outcome,
            "detail": self.detail,
        }


class BayEvent(Base):
    """Evidence that a bay changed state. The audit trail behind ``bays.state``."""

    __tablename__ = "bay_events"
    __table_args__ = (Index("ix_bay_events_bay_ts", "bay_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bay_id: Mapped[str] = mapped_column(ForeignKey("bays.id", ondelete="CASCADE"))
    occupied: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(24), default="simulator")
    confidence: Mapped[float] = mapped_column(default=1.0)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bay_id": self.bay_id,
            "occupied": self.occupied,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "ts": _iso(self.ts),
            "device_id": self.device_id,
        }


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="simulator")
    label: Mapped[str] = mapped_column(String(120), default="")
    last_heartbeat: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    api_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "last_heartbeat": _iso(self.last_heartbeat),
            "status": self.status,
            "meta": self.meta or {},
        }


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actor": self.actor,
            "action": self.action,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "details": self.details or {},
            "ts": _iso(self.ts),
        }


class Conflict(Base):
    """A disagreement between the scan-assigned state and observed occupancy.

    Raised for a human to look at. Nothing here ever moves a vehicle or issues
    a penalty on its own.
    """

    __tablename__ = "conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bay_id: Mapped[str] = mapped_column(ForeignKey("bays.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bay_id": self.bay_id,
            "kind": self.kind,
            "reason": self.reason,
            "details": self.details or {},
            "ts": _iso(self.ts),
            "resolved": self.resolved,
        }


class ArrivalHistory(Base):
    """One row per historical arrival, aggregated per lot and weekday.

    Kept as a per-bucket count rather than one row per vehicle: the predictor
    only needs the distribution, and this keeps the table small enough to
    recompute quickly.
    """

    __tablename__ = "arrival_history"
    __table_args__ = (
        UniqueConstraint(
            "lot_id", "day", "weekday", "bucket_hour", name="uq_arrival_bucket"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    day: Mapped[datetime] = mapped_column(UTCDateTime)
    weekday: Mapped[int] = mapped_column(Integer)
    bucket_hour: Mapped[int] = mapped_column(Integer)
    arrivals: Mapped[int] = mapped_column(Integer, default=0)
    tier_1_arrivals: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "weekday": self.weekday,
            "bucket_hour": self.bucket_hour,
            "arrivals": self.arrivals,
            "tier_1_arrivals": self.tier_1_arrivals,
        }
