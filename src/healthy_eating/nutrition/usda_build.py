"""Builds the bundled USDA reference file from the official FoodData Central downloads.

    uv run python -m healthy_eating.nutrition.usda_build <folder with the three CSV zips>

Download the CSV zips of FNDDS (survey foods), SR Legacy and Foundation Foods from
https://fdc.nal.usda.gov/download-datasets/ into one folder (they stay out of git). The output
keeps, per 100 g of each food, the energy and the nutrients in `micronutrients.py`, and goes to
`nutrition/data/usda_reference.csv.gz` plus `usda_reference.json` (versions, license, hash).
FoodData Central is public domain (CC0 1.0); USDA asks to be credited as the source.
"""

import csv
import gzip
import hashlib
import io
import json
import re
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

from healthy_eating.nutrition.micronutrients import (
    MICRONUTRIENT_INFO,
    MICRONUTRIENTS,
    ReferenceDataset,
)

DATA_DIR = Path(__file__).parent / "data"
CSV_NAME = "usda_reference.csv.gz"
META_NAME = "usda_reference.json"
ENERGY_NUMBER, ENERGY_ID = "208", "1008"
COLUMNS = ["fdc_id", "dataset", "description", "energy_kcal", *MICRONUTRIENTS]

# Zip name pattern, the food.csv `data_type` to keep, and how food_nutrient.csv links nutrients.
SOURCES: dict[ReferenceDataset, tuple[str, str, str]] = {
    "fndds": (r"FoodData_Central_survey_food_csv_(.+)\.zip", "survey_fndds_food", "number"),
    "foundation": (r"FoodData_Central_foundation_food_csv_(.+)\.zip", "foundation_food", "id"),
    "sr_legacy": (r"FoodData_Central_sr_legacy_food_csv_(.+)\.zip", "sr_legacy_food", "id"),
}


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    member = next(n for n in archive.namelist() if n.endswith(f"/{name}") or n == name)
    with archive.open(member) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))


def _read(path: Path, data_type: str, link: str) -> dict[str, dict[str, str]]:
    """Foods of one dataset: fdc_id -> description and the kept nutrient amounts per 100 g."""
    wanted: dict[str, str] = {ENERGY_NUMBER if link == "number" else ENERGY_ID: "energy_kcal"}
    for nutrient, info in MICRONUTRIENT_INFO.items():
        wanted[info.usda_number if link == "number" else info.usda_id] = nutrient
    with zipfile.ZipFile(path) as archive:
        foods = {
            row["fdc_id"]: {"description": row["description"].strip()}
            for row in _rows(archive, "food.csv")
            if row["data_type"] == data_type
        }
        for row in _rows(archive, "food_nutrient.csv"):
            food = foods.get(row["fdc_id"])
            code = wanted.get(row["nutrient_id"])
            if food is not None and code is not None and row["amount"] != "":
                food[code] = row["amount"]
    return foods


def build(folder: Path) -> dict[str, object]:
    versions: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for dataset, (pattern, data_type, link) in SOURCES.items():
        matches = sorted(p for p in folder.iterdir() if re.fullmatch(pattern, p.name))
        if not matches:
            sys.exit(f"Missing the {dataset} zip in {folder}")
        path = matches[-1]
        match = re.fullmatch(pattern, path.name)
        assert match is not None
        versions[dataset] = match.group(1)
        for fdc_id, food in _read(path, data_type, link).items():
            rows.append({"fdc_id": fdc_id, "dataset": dataset, **food})
    rows.sort(key=lambda row: int(row["fdc_id"]))

    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COLUMNS, restval="", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = text.getvalue().encode()
    DATA_DIR.mkdir(exist_ok=True)
    # mtime=0 keeps the file byte-identical when the data is unchanged.
    (DATA_DIR / CSV_NAME).write_bytes(gzip.compress(content, mtime=0))
    meta: dict[str, object] = {
        "source": "USDA FoodData Central, https://fdc.nal.usda.gov/",
        "license": "CC0 1.0 (public domain); credit USDA FoodData Central as the source",
        "datasets": versions,
        "foods": len(rows),
        "version": hashlib.sha256(content).hexdigest()[:16],
    }
    (DATA_DIR / META_NAME).write_text(json.dumps(meta, indent=2) + "\n")
    return meta


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(build(Path(sys.argv[1])), indent=2))
