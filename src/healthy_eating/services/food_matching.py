"""Matching a meal's food items to open reference data, and their vitamins and minerals.

An item is a food and its weight, as the assistant breaks a meal down ("white rice, cooked",
150 g). It is matched by name with Postgres full-text search on the USDA descriptions, which
put the food first and the details after commas ("Rice, white, cooked, no added fat").
An item can also name a reference food by id (`food_id`), e.g. one the assistant picked from a
search, which is then used as it is.

Candidates share a food word with the item (cooking words only rank, since "cooked" alone
matches thousands of foods). Ranking, best first:
1. how many of the item's words the food has; cooking words (cooked, boiled, raw...) do not
   count here, so "boiled rice" can still match "Rice, white, cooked";
2. the food's main part (before the first comma, without a category word such as "Fish" or
   "Beef") only uses words of the item, so "rice" prefers "Rice, ..." over "Rice noodles, ..."
   and "salmon" accepts "Fish, salmon, ...";
3. the cooking word matches (boiled, steamed, poached and stewed count as cooked);
4. fewer processed forms the item did not name (dried, canned, frozen, powder...), so "apple"
   prefers "Apple, raw" over "Apple, dried";
5. a general entry (USDA marks them "NFS", "NS as to ...", "plain" or "raw");
6. fewer extra words (the more general food);
7. the dataset (FNDDS, then Foundation Foods, then SR Legacy).

`matched` needs every item word (cooking words aside); `weak` (at least half) and `none` give no
values, so nothing is invented from a doubtful match. An item with label values (a supplement,
a packaged food) is not matched: its values are used as given, with the status `label`.
Amounts scale linearly with grams; a nutrient the food has no value for stays unknown, it never
counts as zero.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import ReferenceFood
from healthy_eating.nutrition.micronutrients import (
    MICRONUTRIENTS,
    REFERENCE_DATASETS,
    ItemMatchStatus,
    Micronutrient,
)

MatchStatus = ItemMatchStatus
# The best candidates by text rank; common words ("chicken") match a few hundred foods.
CANDIDATES = 1000
WEAK_SHARE = 0.5

# English stems of cooking words; the first group all mean "cooked in water or heat".
COOKED = frozenset({"cook", "boil", "steam", "poach", "stew", "simmer"})
COOKING_WORDS = COOKED | frozenset(
    {"raw", "fri", "bake", "roast", "grill", "broil", "brais", "saute", "toast", "microwav"}
)
# Stems of processed forms, preferred only when the item names them.
PROCESSED = frozenset(
    {"dri", "can", "frozen", "dehydr", "powder", "concentr", "sweeten", "instant", "babi", "infant"}
)
# Category words USDA puts first ("Fish, salmon, raw", "Beef, steak, NFS").
CATEGORIES = frozenset(
    {"fish", "beef", "pork", "chicken", "turkey", "lamb", "veal", "chees", "cereal"}
)
# Stems USDA uses for general entries: "not further specified", "not specified as to ...".
GENERAL = frozenset({"nfs", "ns", "plain", "raw"})


@dataclass(frozen=True)
class FoodItem:
    name: str
    # Weight eaten; may be None only for an item with label values (a capsule).
    grams: float | None
    # A reference food chosen by id; None to match by name.
    food_id: int | None = None
    # Values from the item's label or the user, for the amount taken; used as they are.
    label_values: dict[Micronutrient, float] | None = None


@dataclass(frozen=True)
class FoodCandidate:
    id: int
    dataset: str
    description: str
    energy_kcal: float | None
    nutrients: dict[str, float]
    # Stems of the whole description and of its part before the first comma.
    words: frozenset[str]
    head: frozenset[str]


@dataclass(frozen=True)
class ItemMatch:
    item: FoodItem
    status: MatchStatus
    food: FoodCandidate | None
    # For the item's weight; empty unless matched.
    nutrients: dict[Micronutrient, float] = field(default_factory=dict)


def _cooking_matches(query: frozenset[str], food: frozenset[str]) -> int:
    wanted = query & COOKING_WORDS
    if not wanted:
        return 0
    if wanted & COOKED and food & COOKED:
        return 1
    return 1 if wanted & food else 0


def _rank(query: frozenset[str], food: FoodCandidate) -> tuple[float, int, int, int, int, int, int]:
    core = (query - COOKING_WORDS) or query
    coverage = len(core & food.words) / len(core)
    head_in_query = 1 if food.head - CATEGORIES <= query else 0
    processed = len((food.words & PROCESSED) - query)
    extras = len(food.words - query)
    priority = REFERENCE_DATASETS.index(food.dataset)
    cooking = _cooking_matches(query, food.words)
    general = 1 if food.words & GENERAL else 0
    return (-coverage, -head_in_query, -cooking, processed, -general, extras, priority)


def _status(query: frozenset[str], food: FoodCandidate) -> MatchStatus:
    core = (query - COOKING_WORDS) or query
    coverage = len(core & food.words) / len(core)
    if coverage == 1:
        return "matched"
    return "weak" if coverage >= WEAK_SHARE else "none"


def _candidate(row: Any) -> FoodCandidate:
    return FoodCandidate(
        id=row.id,
        dataset=row.dataset,
        description=row.description,
        energy_kcal=row.energy_kcal,
        nutrients=row.nutrients,
        words=frozenset(row.words),
        head=frozenset(row.head),
    )


def _columns() -> tuple[Any, ...]:
    head = func.to_tsvector("english", func.split_part(ReferenceFood.description, ",", 1))
    return (
        ReferenceFood.id,
        ReferenceFood.dataset,
        ReferenceFood.description,
        ReferenceFood.energy_kcal,
        ReferenceFood.nutrients,
        func.tsvector_to_array(ReferenceFood.search).label("words"),
        func.tsvector_to_array(head).label("head"),
    )


async def query_words(session: AsyncSession, text: str) -> frozenset[str]:
    words = await session.scalar(
        select(func.tsvector_to_array(func.to_tsvector("english", literal(text))))
    )
    return frozenset(words or ())


async def search_foods(
    session: AsyncSession, text: str, *, limit: int = 5
) -> list[tuple[FoodCandidate, MatchStatus]]:
    """Reference foods for a food name, best first (see the module docstring)."""
    words = await query_words(session, text)
    if not words:
        return []
    core = (words - COOKING_WORDS) or words
    # The stems are already normalized: the 'simple' configuration keeps them as they are.
    any_word = func.to_tsquery("simple", " | ".join(_quoted(word) for word in sorted(core)))
    rows = await session.execute(
        select(*_columns())
        .where(ReferenceFood.search.op("@@")(any_word))
        .order_by(func.ts_rank_cd(ReferenceFood.search, any_word).desc(), ReferenceFood.id)
        .limit(CANDIDATES)
    )
    candidates = sorted((_candidate(row) for row in rows), key=lambda food: _rank(words, food))
    return [(food, _status(words, food)) for food in candidates[:limit]]


def _quoted(word: str) -> str:
    return "'" + word.replace("'", "''").replace("\\", "\\\\") + "'"


def _scaled(food: FoodCandidate, grams: float) -> dict[Micronutrient, float]:
    return {
        code: food.nutrients[code] * grams / 100
        for code in MICRONUTRIENTS
        if code in food.nutrients
    }


# Search results by item name, shared by the meals of one import (services/data_import.py), so
# a long history searches each food name once.
SearchCache = dict[str, list[tuple[FoodCandidate, MatchStatus]]]


async def match_items(
    session: AsyncSession, items: list[FoodItem], search_cache: SearchCache | None = None
) -> list[ItemMatch]:
    """Each item's reference food and its vitamins and minerals for the item's weight."""
    by_id: dict[int, FoodCandidate] = {}
    ids = {item.food_id for item in items if item.food_id is not None}
    if ids:
        rows = await session.execute(select(*_columns()).where(ReferenceFood.id.in_(ids)))
        by_id = {row.id: _candidate(row) for row in rows}

    matches = []
    for item in items:
        if item.label_values is not None:
            matches.append(ItemMatch(item, "label", None, dict(item.label_values)))
            continue
        if item.grams is None:
            matches.append(ItemMatch(item, "none", None))
            continue
        if item.food_id is not None:
            food = by_id.get(item.food_id)
            if food is None:
                matches.append(ItemMatch(item, "none", None))
            else:
                matches.append(ItemMatch(item, "matched", food, _scaled(food, item.grams)))
            continue
        cached = None if search_cache is None else search_cache.get(item.name)
        found = cached if cached is not None else await search_foods(session, item.name, limit=1)
        if search_cache is not None:
            search_cache[item.name] = found
        if not found:
            matches.append(ItemMatch(item, "none", None))
            continue
        food, status = found[0]
        nutrients = _scaled(food, item.grams) if status == "matched" else {}
        matches.append(ItemMatch(item, status, food, nutrients))
    return matches


@dataclass(frozen=True)
class MicronutrientTotal:
    amount: float
    # How many of the meal's items have a value for this nutrient.
    items: int


def meal_totals(matches: list[ItemMatch]) -> dict[Micronutrient, MicronutrientTotal]:
    """Sum per nutrient over the matched items that have a value for it."""
    totals: dict[Micronutrient, MicronutrientTotal] = {}
    for code in MICRONUTRIENTS:
        values = [match.nutrients[code] for match in matches if code in match.nutrients]
        if values:
            totals[code] = MicronutrientTotal(amount=sum(values), items=len(values))
    return totals
