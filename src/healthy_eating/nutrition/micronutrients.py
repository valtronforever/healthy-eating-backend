"""Vitamins and minerals (healthy-eating-backend#25): the nutrients, their units and sources.

Values come from open reference data (USDA FoodData Central, CC0) for the food items of a meal,
never from the assistant. Units are canonical: mg, or µg for vitamins A, D, K, folate, B12 and
selenium. Sodium is not here: it is an optional meal value (`sodium_mg`) like fiber and sugar.
"""

from dataclasses import dataclass
from typing import Literal, get_args

Micronutrient = Literal[
    "vitamin_a_ug",
    "vitamin_c_mg",
    "vitamin_d_ug",
    "vitamin_e_mg",
    "vitamin_k_ug",
    "thiamin_mg",
    "riboflavin_mg",
    "niacin_mg",
    "vitamin_b6_mg",
    "folate_ug",
    "vitamin_b12_ug",
    "calcium_mg",
    "iron_mg",
    "magnesium_mg",
    "phosphorus_mg",
    "potassium_mg",
    "zinc_mg",
    "selenium_ug",
]
MICRONUTRIENTS: tuple[Micronutrient, ...] = get_args(Micronutrient)


@dataclass(frozen=True)
class MicronutrientInfo:
    label: str
    unit: Literal["mg", "µg"]
    # USDA nutrient number (`nutrient_nbr`) and id; FNDDS files link nutrients by number,
    # SR Legacy and Foundation Foods by id.
    usda_number: str
    usda_id: str


MICRONUTRIENT_INFO: dict[Micronutrient, MicronutrientInfo] = {
    "vitamin_a_ug": MicronutrientInfo("Vitamin A (RAE)", "µg", "320", "1106"),
    "vitamin_c_mg": MicronutrientInfo("Vitamin C", "mg", "401", "1162"),
    "vitamin_d_ug": MicronutrientInfo("Vitamin D", "µg", "328", "1114"),
    "vitamin_e_mg": MicronutrientInfo("Vitamin E", "mg", "323", "1109"),
    "vitamin_k_ug": MicronutrientInfo("Vitamin K", "µg", "430", "1185"),
    "thiamin_mg": MicronutrientInfo("Thiamin (B1)", "mg", "404", "1165"),
    "riboflavin_mg": MicronutrientInfo("Riboflavin (B2)", "mg", "405", "1166"),
    "niacin_mg": MicronutrientInfo("Niacin (B3)", "mg", "406", "1167"),
    "vitamin_b6_mg": MicronutrientInfo("Vitamin B6", "mg", "415", "1175"),
    "folate_ug": MicronutrientInfo("Folate (DFE)", "µg", "435", "1190"),
    "vitamin_b12_ug": MicronutrientInfo("Vitamin B12", "µg", "418", "1178"),
    "calcium_mg": MicronutrientInfo("Calcium", "mg", "301", "1087"),
    "iron_mg": MicronutrientInfo("Iron", "mg", "303", "1089"),
    "magnesium_mg": MicronutrientInfo("Magnesium", "mg", "304", "1090"),
    "phosphorus_mg": MicronutrientInfo("Phosphorus", "mg", "305", "1091"),
    "potassium_mg": MicronutrientInfo("Potassium", "mg", "306", "1092"),
    "zinc_mg": MicronutrientInfo("Zinc", "mg", "309", "1095"),
    "selenium_ug": MicronutrientInfo("Selenium", "µg", "317", "1103"),
}

# Reference datasets, best first when two foods match a query equally well: FNDDS describes
# foods as eaten ("Rice, white, cooked, no added fat"), Foundation Foods are newer analyses,
# SR Legacy is the broad final release of the old Standard Reference.
ReferenceDataset = Literal["fndds", "foundation", "sr_legacy"]
REFERENCE_DATASETS: tuple[ReferenceDataset, ...] = get_args(ReferenceDataset)

# How a meal's food item matched the reference data (services/food_matching.py): only
# `matched` items have values.
ItemMatchStatus = Literal["matched", "weak", "none"]
ITEM_MATCH_STATUSES: tuple[ItemMatchStatus, ...] = get_args(ItemMatchStatus)

# Optional reference group for vitamin and mineral reference ranges (Settings; off by default).
ReferenceGroup = Literal["adult_women", "adult_men"]
REFERENCE_GROUPS: tuple[ReferenceGroup, ...] = get_args(ReferenceGroup)


@dataclass(frozen=True)
class ReferenceRange:
    # RDA, or AI where no RDA exists.
    min: float
    # Tolerable upper intake level, only where it applies to food; None otherwise.
    max: float | None


# US Dietary Reference Intakes (National Academies), adults 19 to 50, as published by the NIH
# Office of Dietary Supplements (ods.od.nih.gov, public domain). Upper limits that apply only to
# supplements or synthetic forms (vitamin E, niacin, folate, magnesium, preformed vitamin A) are
# left out: comparing food totals with them would mislead. Potassium uses the 2019 AI; magnesium
# the 31 to 50 RDA. Vitamin K and potassium are AIs. Neutral reference values, never targets.
_SHARED: dict[Micronutrient, ReferenceRange] = {
    "vitamin_d_ug": ReferenceRange(15, 100),
    "vitamin_e_mg": ReferenceRange(15, None),
    "vitamin_b6_mg": ReferenceRange(1.3, 100),
    "folate_ug": ReferenceRange(400, None),
    "vitamin_b12_ug": ReferenceRange(2.4, None),
    "calcium_mg": ReferenceRange(1000, 2500),
    "phosphorus_mg": ReferenceRange(700, 4000),
    "selenium_ug": ReferenceRange(55, 400),
}
REFERENCE_INTAKES: dict[ReferenceGroup, dict[Micronutrient, ReferenceRange]] = {
    "adult_women": {
        **_SHARED,
        "vitamin_a_ug": ReferenceRange(700, None),
        "vitamin_c_mg": ReferenceRange(75, 2000),
        "vitamin_k_ug": ReferenceRange(90, None),
        "thiamin_mg": ReferenceRange(1.1, None),
        "riboflavin_mg": ReferenceRange(1.1, None),
        "niacin_mg": ReferenceRange(14, None),
        "iron_mg": ReferenceRange(18, 45),
        "magnesium_mg": ReferenceRange(320, None),
        "potassium_mg": ReferenceRange(2600, None),
        "zinc_mg": ReferenceRange(8, 40),
    },
    "adult_men": {
        **_SHARED,
        "vitamin_a_ug": ReferenceRange(900, None),
        "vitamin_c_mg": ReferenceRange(90, 2000),
        "vitamin_k_ug": ReferenceRange(120, None),
        "thiamin_mg": ReferenceRange(1.2, None),
        "riboflavin_mg": ReferenceRange(1.3, None),
        "niacin_mg": ReferenceRange(16, None),
        "iron_mg": ReferenceRange(8, 45),
        "magnesium_mg": ReferenceRange(420, None),
        "potassium_mg": ReferenceRange(3400, None),
        "zinc_mg": ReferenceRange(11, 40),
    },
}
