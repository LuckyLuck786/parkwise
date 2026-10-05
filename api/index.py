"""Vercel serverless entrypoint.

``@vercel/python`` bundles only this directory by default, so the backend
package is pulled in explicitly through ``functions.includeFiles`` in
vercel.json. Those paths stay repo-relative inside the bundle, so the app is
looked up by searching rather than by guessing a single location.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _locate_backend() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent,                                   # repo root in dev
        here.parent / "backend",                       # includeFiles layout
        here / "backend",
    ]
    for root in candidates:
        if (root / "app" / "main.py").is_file():
            return root
    searched = ", ".join(str(c) for c in candidates)
    raise RuntimeError(
        "Could not locate the ParkWise backend package. Looked for app/main.py "
        f"under: {searched}. Check functions.includeFiles in vercel.json."
    )


_ROOT = _locate_backend()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import app  # noqa: E402

__all__ = ["app"]
