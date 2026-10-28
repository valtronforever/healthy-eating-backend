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

# How a meal's food item got its values (services/food_matching.py): `matched` from the USDA
# reference data, `label` from its label or the user (supplements, packaged foods); `weak` and
# `none` have no values.
ItemMatchStatus = Literal["matched", "weak", "none", "label"]
ITEM_MATCH_STATUSES: tuple[ItemMatchStatus, ...] = get_args(ItemMatchStatus)

# Optional life stage for vitamin and mineral reference ranges (Settings; off by default). The
# user picks it; no birth date or age is stored (owner decision 2026-10-27, #34). Adults only.
ReferenceGroup = Literal[
    "women_19_30",
    "women_31_50",
    "women_51_70",
    "women_71_plus",
    "men_19_30",
    "men_31_50",
    "men_51_70",
    "men_71_plus",
    "pregnancy_19_30",
    "pregnancy_31_50",
    "breastfeeding_19_30",
    "breastfeeding_31_50",
]
REFERENCE_GROUPS: tuple[ReferenceGroup, ...] = get_args(ReferenceGroup)

# The two groups before #34 used the values for ages 31 to 50; older apps may still send them.
LegacyReferenceGroup = Literal["adult_women", "adult_men"]
LEGACY_REFERENCE_GROUPS: dict[LegacyReferenceGroup, ReferenceGroup] = {
    "adult_women": "women_31_50",
    "adult_men": "men_31_50",
}


@dataclass(frozen=True)
class ReferenceRange:
    # RDA, or AI where no RDA exists.
    min: float
    # Tolerable upper intake level, only where it applies to food; None otherwise.
    max: float | None


# US Dietary Reference Intakes (National Academies) as published by the NIH Office of Dietary
# Supplements in its health professional fact sheets (ods.od.nih.gov, public domain; read
# 2026-10-28). One column per group, in the order of REFERENCE_GROUPS:
#   women 19-30, 31-50, 51-70, 71+ | men 19-30, 31-50, 51-70, 71+ |
#   pregnancy 19-30, 31-50 | breastfeeding 19-30, 31-50
# Minimums are RDAs, or AIs for vitamin K and potassium (the 2019 AI). Upper limits that apply
# only to supplements or synthetic forms (vitamin E, niacin, folate, magnesium, preformed
# vitamin A) are left out: comparing food totals with them would mislead. Vitamin K and B12,
# thiamin, riboflavin and potassium have no upper limit. Neutral reference values, never targets.
_MINIMUMS: dict[Micronutrient, tuple[float, ...]] = {
    "vitamin_a_ug": (700, 700, 700, 700, 900, 900, 900, 900, 770, 770, 1300, 1300),
    "vitamin_c_mg": (75, 75, 75, 75, 90, 90, 90, 90, 85, 85, 120, 120),
    "vitamin_d_ug": (15, 15, 15, 20, 15, 15, 15, 20, 15, 15, 15, 15),
    "vitamin_e_mg": (15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 19, 19),
    "vitamin_k_ug": (90, 90, 90, 90, 120, 120, 120, 120, 90, 90, 90, 90),
    "thiamin_mg": (1.1, 1.1, 1.1, 1.1, 1.2, 1.2, 1.2, 1.2, 1.4, 1.4, 1.4, 1.4),
    "riboflavin_mg": (1.1, 1.1, 1.1, 1.1, 1.3, 1.3, 1.3, 1.3, 1.4, 1.4, 1.6, 1.6),
    "niacin_mg": (14, 14, 14, 14, 16, 16, 16, 16, 18, 18, 17, 17),
    "vitamin_b6_mg": (1.3, 1.3, 1.5, 1.5, 1.3, 1.3, 1.7, 1.7, 1.9, 1.9, 2.0, 2.0),
    "folate_ug": (400, 400, 400, 400, 400, 400, 400, 400, 600, 600, 500, 500),
    "vitamin_b12_ug": (2.4, 2.4, 2.4, 2.4, 2.4, 2.4, 2.4, 2.4, 2.6, 2.6, 2.8, 2.8),
    "calcium_mg": (1000, 1000, 1200, 1200, 1000, 1000, 1000, 1200, 1000, 1000, 1000, 1000),
    "iron_mg": (18, 18, 8, 8, 8, 8, 8, 8, 27, 27, 9, 9),
    "magnesium_mg": (310, 320, 320, 320, 400, 420, 420, 420, 350, 360, 310, 320),
    "phosphorus_mg": (700, 700, 700, 700, 700, 700, 700, 700, 700, 700, 700, 700),
    "potassium_mg": (2600, 2600, 2600, 2600, 3400, 3400, 3400, 3400, 2900, 2900, 2800, 2800),
    "zinc_mg": (8, 8, 8, 8, 11, 11, 11, 11, 11, 11, 12, 12),
    "selenium_ug": (55, 55, 55, 55, 55, 55, 55, 55, 60, 60, 70, 70),
}
_MAXIMUMS: dict[Micronutrient, tuple[float, ...]] = {
    "vitamin_c_mg": (2000,) * 12,
    "vitamin_d_ug": (100,) * 12,
    "vitamin_b6_mg": (100,) * 12,
    "calcium_mg": (2500, 2500, 2000, 2000, 2500, 2500, 2000, 2000, 2500, 2500, 2500, 2500),
    "iron_mg": (45,) * 12,
    "phosphorus_mg": (4000, 4000, 4000, 3000, 4000, 4000, 4000, 3000, 3500, 3500, 4000, 4000),
    "zinc_mg": (40,) * 12,
    "selenium_ug": (400,) * 12,
}
REFERENCE_INTAKES: dict[ReferenceGroup, dict[Micronutrient, ReferenceRange]] = {
    group: {
        code: ReferenceRange(
            minimums[column], _MAXIMUMS[code][column] if code in _MAXIMUMS else None
        )
        for code, minimums in _MINIMUMS.items()
    }
    for column, group in enumerate(REFERENCE_GROUPS)
}
