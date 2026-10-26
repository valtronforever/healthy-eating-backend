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
