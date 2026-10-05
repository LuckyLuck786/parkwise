"""The demo clock.

Every time-dependent decision in ParkWise - grace periods, tier-2 cut-offs,
prediction horizons - reads ``now(db)`` and never ``datetime.now()`` directly.
That is what lets a judge watch a whole morning rush in a couple of minutes,
and it is why the allocation tests can pin an instant instead of sleeping.

The clock lives in ``app_state`` so it survives between serverless requests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import get_state, set_state

CLOCK_KEY = "demo_clock"
SPEEDS = (1, 60, 300, 1800)


def _default_state() -> dict[str, Any]:
    return {
        "mode": "live",
        "speed": 1.0,
        "base": datetime.now(UTC).isoformat(),
        "offset_seconds": 0.0,
        "playing": True,
    }


def read_state(db: Session) -> dict[str, Any]:
    raw = get_state(db, CLOCK_KEY)
    if isinstance(raw, dict):
        return {**_default_state(), **raw}
    if isinstance(raw, str):
        try:
            return {**_default_state(), **json.loads(raw)}
        except (TypeError, ValueError):
            return _default_state()
    return _default_state()


def write_state(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    merged = {**_default_state(), **payload}
    set_state(db, CLOCK_KEY, merged)
    db.commit()
    return merged


def now(db: Session) -> datetime:
    """The demo 'now'. The only clock the business logic trusts."""
    state = read_state(db)
    if state.get("mode") != "demo":
        return datetime.now(UTC)
    base = datetime.fromisoformat(state["base"])
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - base).total_seconds() * float(state.get("speed", 1.0))
    return base + timedelta(seconds=float(state.get("offset_seconds", 0.0)) + elapsed)


def set_clock(
    db: Session,
    *,
    mode: str | None = None,
    speed: float | None = None,
    jump_to: datetime | str | None = None,
    playing: bool | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Drive the clock. Every field is optional; omitted fields are unchanged."""
    state = _default_state() if reset else read_state(db)

    if reset:
        state["mode"] = "demo"
        state["speed"] = 300.0
        state["base"] = datetime.now(UTC).isoformat()
        state["offset_seconds"] = 0.0
        state["playing"] = True
        # Start the demo at today's 08:00 so the morning rush is watchable.
        anchor = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)
        state["base"] = anchor.isoformat()

    if mode is not None:
        state["mode"] = mode
        if mode == "demo" and "base" not in state:
            state["base"] = datetime.now(UTC).isoformat()
    if speed is not None:
        state["speed"] = float(speed)
    if jump_to is not None:
        target = jump_to if isinstance(jump_to, datetime) else datetime.fromisoformat(jump_to)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        current = now(db)
        # Keep the instant, not the base, so a speed change cannot shift it.
        state["mode"] = "demo"
        state["offset_seconds"] = (target - current).total_seconds()
    if playing is not None:
        state["playing"] = playing

    return write_state(db, state)


def clock_payload(db: Session) -> dict[str, Any]:
    state = read_state(db)
    current = now(db)
    return {
        **state,
        "now": current.isoformat(),
        "demo_date": current.date().isoformat(),
        "weekday": current.weekday(),
        "hour": current.hour,
        "label": (
            "live (real wall-clock time)"
            if state.get("mode") != "demo"
            else f"demo clock at x{state.get('speed', 1.0)} - {current:%H:%M}"
        ),
    }
