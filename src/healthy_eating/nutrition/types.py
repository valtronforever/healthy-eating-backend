"""Shared nutrition vocabulary, used by the database models, services and tools."""

from typing import Literal, get_args

Confidence = Literal["high", "medium", "low"]
CONFIDENCE_VALUES: tuple[Confidence, ...] = get_args(Confidence)

# "estimated_by_assistant": the assistant estimated the values itself, from a photo or text.
# "from_conversation": (some of) the values were already calculated before, by the user's
# own workflow (a ChatGPT project, a spreadsheet, a nutrition label, another app).
MealSource = Literal["estimated_by_assistant", "from_conversation"]
MEAL_SOURCES: tuple[MealSource, ...] = get_args(MealSource)

NutrientField = Literal["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"]
NUTRIENT_FIELDS: tuple[NutrientField, ...] = get_args(NutrientField)

# Unit the user wants energy shown in. Energy is always stored in kcal.
EnergyUnit = Literal["kcal", "kj"]
ENERGY_UNITS: tuple[EnergyUnit, ...] = get_args(EnergyUnit)

# Whether all meals of a local day were logged. Set by the user (app) or the assistant (MCP).
DayStatus = Literal["complete", "partial"]
DAY_STATUSES: tuple[DayStatus, ...] = get_args(DayStatus)
StatusSetter = Literal["user", "assistant"]
STATUS_SETTERS: tuple[StatusSetter, ...] = get_args(StatusSetter)
