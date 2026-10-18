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
