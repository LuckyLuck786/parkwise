"""Response contracts for the initial ParkWise API surface."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    status: str
    app: str
    version: str
    environment: str
    database: str
    nodes: bool = True


class BuildingOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    x: float
    y: float
    has_step_free_entrance: bool = True
    notes: str | None = None


class BayOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    lot_id: str
    label: str
    bay_type: str
    is_accessible: bool
    reserved_tier: int
    x: float
    y: float
    width: float
    height: float
    nearest_building_id: str | None = None
    state: str
    device_id: str | None = None
    sensor_fresh: bool = True
    occupied_since: str | None = None
    low_confidence: bool = False


class LotSummary(BaseModel):
    id: str
    name: str
    capacity: int
    free_bays: int
    occupied_bays: int
    allotted_bays: int
    blocked_bays: int
    low_confidence_bays: int
    accessible_bays: int
    fill_ratio: float
    x: float
    y: float


class LotOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    lot: LotSummary
    bays: list[BayOut] = Field(default_factory=list)


class LotsOut(BaseModel):
    lots: list[LotOut]
    buildings: list[BuildingOut] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    generated_at: str
    simulated: bool = True
    note: str = (
        "Occupancy is simulated. Bay state comes from the simulator by default "
        "and from IR sensors or a webcam only when a device is attached."
    )


class VehicleOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    user_id: int
    plate_or_tag_id: str
    vehicle_type: str
    label: str
    active_today: bool
