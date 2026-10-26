"""Loads the bundled open reference data into the database (healthy-eating-backend#25).

The USDA file (`nutrition/data/usda_reference.csv.gz`, built by `nutrition/usda_build.py`) is
loaded at startup when the database has no copy or an older one, in one transaction: queries
see either the old foods or the new ones. A transaction-level advisory lock keeps two app
processes from loading at the same time.
"""

import csv
import gzip
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import ReferenceData, ReferenceFood
from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS

DATA_DIR = Path(__file__).resolve().parent.parent / "nutrition" / "data"
USDA = "usda"
# Any constant; only this module takes the lock.
LOAD_LOCK = 25_001
BATCH_SIZE = 2000


@dataclass(frozen=True)
class BundledData:
    version: str
    datasets: dict[str, str]
    rows: list[dict[str, Any]]


def bundled_usda(data_dir: Path = DATA_DIR) -> BundledData:
    meta = json.loads((data_dir / "usda_reference.json").read_text())
    rows = []
    with gzip.open(data_dir / "usda_reference.csv.gz", "rt", encoding="utf-8") as file:
        for row in csv.DictReader(io.StringIO(file.read())):
            rows.append(
                {
                    "id": int(row["fdc_id"]),
                    "dataset": row["dataset"],
                    "description": row["description"],
                    "energy_kcal": float(row["energy_kcal"]) if row["energy_kcal"] else None,
                    "nutrients": {
                        code: float(row[code]) for code in MICRONUTRIENTS if row[code] != ""
                    },
                }
            )
    return BundledData(version=meta["version"], datasets=meta["datasets"], rows=rows)


async def ensure_reference_data(session: AsyncSession, bundled: BundledData | None = None) -> bool:
    """Load the bundled USDA foods unless this version is loaded already; True when loaded."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOAD_LOCK})
    loaded = await session.get(ReferenceData, USDA)
    data = bundled or bundled_usda()
    if loaded is not None and loaded.version == data.version:
        return False
    await session.execute(delete(ReferenceFood))
    for start in range(0, len(data.rows), BATCH_SIZE):
        await session.execute(insert(ReferenceFood), data.rows[start : start + BATCH_SIZE])
    if loaded is None:
        loaded = ReferenceData(name=USDA)
        session.add(loaded)
    loaded.version = data.version
    loaded.datasets = data.datasets
    loaded.foods = len(data.rows)
    loaded.loaded_at = func.now()
    await session.flush()
    return True


async def reference_food_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(ReferenceFood)) or 0
