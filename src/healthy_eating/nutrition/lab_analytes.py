"""Lab results (healthy-eating-backend#37): the analytes we know, their units and conversions.

A lab result is stored as printed on the report (value, unit, the lab's reference range). For a
known analyte in a unit we can convert, it also gets the value in the analyte's canonical unit,
so results from labs that print different units can be compared over time. Other analytes and
units are stored as printed, without a canonical value.

Conversion factors are molar masses and the IFCC/NGSP master equation for HbA1c; they are
facts about units, not about health, and nothing here judges a value.
"""

import re
from dataclasses import dataclass, field
from typing import Literal, get_args

from healthy_eating.nutrition.micronutrients import Micronutrient

LabAnalyte = Literal[
    "vitamin_d_25oh",
    "vitamin_b12",
    "folate",
    "ferritin",
    "iron",
    "transferrin_saturation",
    "hemoglobin",
    "magnesium",
    "calcium",
    "potassium",
    "sodium",
    "zinc",
    "selenium",
    "cholesterol_total",
    "ldl_cholesterol",
    "hdl_cholesterol",
    "triglycerides",
    "glucose",
    "hba1c",
    "tsh",
]
LAB_ANALYTES: tuple[LabAnalyte, ...] = get_args(LabAnalyte)

# A value printed with a sign, like "< 5" (below the lab's limit of detection) or "> 1500".
LabComparator = Literal["<", ">"]
LAB_COMPARATORS: tuple[LabComparator, ...] = get_args(LabComparator)


@dataclass(frozen=True)
class UnitConversion:
    """canonical = value * factor + offset."""

    factor: float
    offset: float = 0.0


@dataclass(frozen=True)
class AnalyteInfo:
    label: str
    canonical_unit: str
    # By unit_key(); the canonical unit is always there with factor 1.
    conversions: dict[str, UnitConversion] = field(default_factory=dict)
    # The vitamin or mineral the user can set a goal for, where there is one.
    related_nutrient: Micronutrient | None = None


def _units(**factors: float | UnitConversion) -> dict[str, UnitConversion]:
    return {
        unit.replace("_per_", "/").replace("pct", "%"): (
            value if isinstance(value, UnitConversion) else UnitConversion(value)
        )
        for unit, value in factors.items()
    }


# Cholesterol (386.65 g/mol), triglycerides (as triolein, 885.7 g/mol), glucose (180.16 g/mol).
_CHOLESTEROL = _units(mmol_per_l=1, mg_per_dl=0.02586)

ANALYTE_INFO: dict[LabAnalyte, AnalyteInfo] = {
    # 400.6 g/mol: 1 ng/mL = 2.496 nmol/L.
    "vitamin_d_25oh": AnalyteInfo(
        "25-OH vitamin D",
        "nmol/L",
        _units(nmol_per_l=1, ng_per_ml=2.496, ug_per_l=2.496),
        "vitamin_d_ug",
    ),
    # 1355.4 g/mol: 1 pg/mL = 0.7378 pmol/L.
    "vitamin_b12": AnalyteInfo(
        "Vitamin B12",
        "pmol/L",
        _units(pmol_per_l=1, pg_per_ml=0.7378, ng_per_l=0.7378),
        "vitamin_b12_ug",
    ),
    # 441.4 g/mol: 1 ng/mL = 2.266 nmol/L.
    "folate": AnalyteInfo(
        "Folate",
        "nmol/L",
        _units(nmol_per_l=1, ng_per_ml=2.266, ug_per_l=2.266),
        "folate_ug",
    ),
    "ferritin": AnalyteInfo("Ferritin", "µg/L", _units(ug_per_l=1, ng_per_ml=1), "iron_mg"),
    # 55.845 g/mol: 1 µg/dL = 0.1791 µmol/L.
    "iron": AnalyteInfo("Serum iron", "µmol/L", _units(umol_per_l=1, ug_per_dl=0.1791), "iron_mg"),
    "transferrin_saturation": AnalyteInfo("Transferrin saturation", "%", _units(pct=1), "iron_mg"),
    # As the monomer, 16,114.5 g/mol: 1 mmol/L = 16.11 g/L.
    "hemoglobin": AnalyteInfo(
        "Hemoglobin", "g/L", _units(g_per_l=1, g_per_dl=10, mmol_per_l=16.11)
    ),
    # 24.305 g/mol; divalent, so 1 mEq/L = 0.5 mmol/L.
    "magnesium": AnalyteInfo(
        "Magnesium",
        "mmol/L",
        _units(mmol_per_l=1, mg_per_dl=0.4114, meq_per_l=0.5),
        "magnesium_mg",
    ),
    # 40.078 g/mol; divalent.
    "calcium": AnalyteInfo(
        "Calcium",
        "mmol/L",
        _units(mmol_per_l=1, mg_per_dl=0.2495, meq_per_l=0.5),
        "calcium_mg",
    ),
    "potassium": AnalyteInfo(
        "Potassium", "mmol/L", _units(mmol_per_l=1, meq_per_l=1), "potassium_mg"
    ),
    # Sodium is a meal value without a goal (nutrition/types.py), so it has no related nutrient.
    "sodium": AnalyteInfo("Sodium", "mmol/L", _units(mmol_per_l=1, meq_per_l=1)),
    # 65.38 g/mol: 1 µg/dL = 0.1530 µmol/L.
    "zinc": AnalyteInfo(
        "Zinc", "µmol/L", _units(umol_per_l=1, ug_per_dl=0.153, ug_per_l=0.0153), "zinc_mg"
    ),
    # 78.97 g/mol: 1 µmol/L = 78.97 µg/L.
    "selenium": AnalyteInfo(
        "Selenium", "µg/L", _units(ug_per_l=1, ng_per_ml=1, umol_per_l=78.97), "selenium_ug"
    ),
    "cholesterol_total": AnalyteInfo("Total cholesterol", "mmol/L", _CHOLESTEROL),
    "ldl_cholesterol": AnalyteInfo("LDL cholesterol", "mmol/L", _CHOLESTEROL),
    "hdl_cholesterol": AnalyteInfo("HDL cholesterol", "mmol/L", _CHOLESTEROL),
    "triglycerides": AnalyteInfo(
        "Triglycerides", "mmol/L", _units(mmol_per_l=1, mg_per_dl=0.01129)
    ),
    "glucose": AnalyteInfo("Glucose", "mmol/L", _units(mmol_per_l=1, mg_per_dl=0.05551)),
    # NGSP % = 0.09148 x IFCC mmol/mol + 2.152 (the IFCC-NGSP master equation).
    "hba1c": AnalyteInfo("HbA1c", "%", _units(pct=1, mmol_per_mol=UnitConversion(0.09148, 2.152))),
    "tsh": AnalyteInfo("TSH", "mIU/L", _units(miu_per_l=1, uiu_per_ml=1, mu_per_l=1)),
}

_SPACES = re.compile(r"\s+")


def unit_key(unit: str) -> str:
    """A unit as printed, compared without case, spaces or the spelling of micro."""
    key = _SPACES.sub("", unit).casefold()
    return key.replace("µ", "u").replace("μ", "u").replace("mcg", "ug")


def conversion(analyte: LabAnalyte, unit: str | None) -> UnitConversion | None:
    """How to convert a printed unit to the analyte's canonical unit; None when we cannot."""
    if unit is None:
        return None
    return ANALYTE_INFO[analyte].conversions.get(unit_key(unit))


def to_canonical(value: float, how: UnitConversion) -> float:
    return round(value * how.factor + how.offset, 3)
