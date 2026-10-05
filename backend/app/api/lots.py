"""Public read-only lot status.

This is the endpoint the live lot map polls. It is deliberately cheap: one
query for lots, one for bays, one for buildings, and the counts are computed
from the rows already in hand rather than with extra COUNT queries.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DbSession
from app.db.models import (
    BAY_ALLOTTED,
    BAY_BLOCKED,
    BAY_FREE,
    BAY_OCCUPIED,
    Bay,
    Building,
    Lot,
)
from app.schemas.core import BuildingOut, LotOut, LotsOut, LotSummary
from app.services import demo_clock

router = APIRouter(tags=["lots"])


def _summarise(lot: Lot, bays: list[Bay]) -> LotSummary:
    counts = {BAY_FREE: 0, BAY_OCCUPIED: 0, BAY_ALLOTTED: 0, BAY_BLOCKED: 0}
    accessible = 0
    low_confidence = 0
    for bay in bays:
        counts[bay.state] = counts.get(bay.state, 0) + 1
        if bay.is_accessible:
            accessible += 1
        if not bay.sensor_fresh:
            low_confidence += 1
    usable = len(bays) - counts[BAY_BLOCKED]
    return LotSummary(
        id=lot.id,
        name=lot.name,
        capacity=lot.capacity,
        free_bays=counts[BAY_FREE],
        occupied_bays=counts[BAY_OCCUPIED],
        allotted_bays=counts[BAY_ALLOTTED],
        blocked_bays=counts[BAY_BLOCKED],
        low_confidence_bays=low_confidence,
        accessible_bays=accessible,
        fill_ratio=round(counts[BAY_OCCUPIED] / usable, 4) if usable else 0.0,
        x=lot.x,
        y=lot.y,
    )


@router.get("/lots", response_model=LotsOut, summary="Live state of every lot and bay")
def list_lots(db: DbSession) -> LotsOut:
    lots = db.scalars(select(Lot).order_by(Lot.name)).all()
    bays = db.scalars(select(Bay).order_by(Bay.lot_id, Bay.id)).all()
    buildings = db.scalars(select(Building).order_by(Building.name)).all()

    by_lot: dict[str, list[Bay]] = {}
    for bay in bays:
        by_lot.setdefault(bay.lot_id, []).append(bay)

    payload = [LotOut(lot=_summarise(lot, by_lot.get(lot.id, [])), bays=[b.to_dict() for b in by_lot.get(lot.id, [])]) for lot in lots]

    totals: dict[str, int] = {"bays": len(bays), "free": 0, "occupied": 0, "allotted": 0, "blocked": 0}
    for entry in payload:
        totals["free"] += entry.lot.free_bays
        totals["occupied"] += entry.lot.occupied_bays
        totals["allotted"] += entry.lot.allotted_bays
        totals["blocked"] += entry.lot.blocked_bays

    return LotsOut(
        lots=payload,
        buildings=[b.to_dict() for b in buildings],
        totals=totals,
        generated_at=demo_clock.now(db).isoformat(),
    )


@router.get("/lots/{lot_id}/bays", summary="Bays for one lot")
def lot_bays(lot_id: str, db: DbSession) -> dict[str, Any]:
    lot = db.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail=f"Lot '{lot_id}' not found")
    bays = db.scalars(select(Bay).where(Bay.lot_id == lot_id).order_by(Bay.id)).all()
    return {
        "lot": _summarise(lot, bays).model_dump(),
        "bays": [bay.to_dict() for bay in bays],
    }


@router.get("/buildings", summary="Campus buildings")
def list_buildings(db: DbSession) -> dict[str, Any]:
    buildings = db.scalars(select(Building).order_by(Building.name)).all()
    return {"buildings": [BuildingOut(**b.to_dict()).model_dump() for b in buildings]}
