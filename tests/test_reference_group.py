"""The optional life stage for vitamin and mineral reference ranges (#25, #34)."""

from typing import Any

import httpx
import pytest

from healthy_eating.nutrition.micronutrients import (
    MICRONUTRIENTS,
    REFERENCE_GROUPS,
    REFERENCE_INTAKES,
    ReferenceGroup,
    ReferenceRange,
)
from tests.test_meals_api import Account, account


async def settings(http: httpx.AsyncClient, owner: Account, **changes: Any) -> dict[str, Any]:
    if changes:
        response = await http.patch("/api/v1/me/settings", json=changes, headers=owner.headers)
    else:
        response = await http.get("/api/v1/me/settings", headers=owner.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize("group", REFERENCE_GROUPS)
def test_every_group_covers_every_nutrient_with_sensible_ranges(group: ReferenceGroup) -> None:
    references = REFERENCE_INTAKES[group]
    assert set(references) == set(MICRONUTRIENTS)
    for reference in references.values():
        assert reference.min > 0
        assert reference.max is None or reference.max > reference.min


# A few values per group as the NIH ODS fact sheets print them (read 2026-10-28): where life
# stages differ, and the upper limits that change with age or pregnancy.
FACT_SHEETS: dict[ReferenceGroup, dict[str, ReferenceRange]] = {
    "women_19_30": {"magnesium_mg": ReferenceRange(310, None), "iron_mg": ReferenceRange(18, 45)},
    "women_31_50": {"magnesium_mg": ReferenceRange(320, None), "zinc_mg": ReferenceRange(8, 40)},
    "women_51_70": {
        "iron_mg": ReferenceRange(8, 45),
        "calcium_mg": ReferenceRange(1200, 2000),
        "vitamin_b6_mg": ReferenceRange(1.5, 100),
    },
    "women_71_plus": {
        "vitamin_d_ug": ReferenceRange(20, 100),
        "phosphorus_mg": ReferenceRange(700, 3000),
    },
    "men_19_30": {"magnesium_mg": ReferenceRange(400, None), "zinc_mg": ReferenceRange(11, 40)},
    "men_31_50": {
        "magnesium_mg": ReferenceRange(420, None),
        "vitamin_k_ug": ReferenceRange(120, None),
    },
    "men_51_70": {
        "calcium_mg": ReferenceRange(1000, 2000),
        "vitamin_b6_mg": ReferenceRange(1.7, 100),
    },
    "men_71_plus": {
        "calcium_mg": ReferenceRange(1200, 2000),
        "vitamin_d_ug": ReferenceRange(20, 100),
    },
    "pregnancy_19_30": {
        "iron_mg": ReferenceRange(27, 45),
        "folate_ug": ReferenceRange(600, None),
        "magnesium_mg": ReferenceRange(350, None),
        "phosphorus_mg": ReferenceRange(700, 3500),
    },
    "pregnancy_31_50": {
        "magnesium_mg": ReferenceRange(360, None),
        "potassium_mg": ReferenceRange(2900, None),
        "vitamin_a_ug": ReferenceRange(770, None),
    },
    "breastfeeding_19_30": {
        "vitamin_a_ug": ReferenceRange(1300, None),
        "iron_mg": ReferenceRange(9, 45),
        "vitamin_c_mg": ReferenceRange(120, 2000),
    },
    "breastfeeding_31_50": {
        "selenium_ug": ReferenceRange(70, 400),
        "zinc_mg": ReferenceRange(12, 40),
        "vitamin_e_mg": ReferenceRange(19, None),
    },
}


@pytest.mark.parametrize("group", REFERENCE_GROUPS)
def test_values_match_the_fact_sheets(group: ReferenceGroup) -> None:
    for code, expected in FACT_SHEETS[group].items():
        assert REFERENCE_INTAKES[group][code] == expected, code  # type: ignore[index]


def test_the_31_to_50_groups_keep_the_values_of_the_two_earlier_groups() -> None:
    # The values before #34 for `adult_women` and `adult_men`, which now map to these groups.
    shared = {"vitamin_d_ug": (15, 100), "vitamin_e_mg": (15, None), "folate_ug": (400, None)}
    women = {
        **shared,
        "iron_mg": (18, 45),
        "magnesium_mg": (320, None),
        "potassium_mg": (2600, None),
    }
    men = {**shared, "iron_mg": (8, 45), "magnesium_mg": (420, None), "potassium_mg": (3400, None)}
    for group, values in (("women_31_50", women), ("men_31_50", men)):
        for code, (low, high) in values.items():
            assert REFERENCE_INTAKES[group][code] == ReferenceRange(low, high)  # type: ignore[index]


async def test_reference_values_are_off_until_a_group_is_chosen(http: httpx.AsyncClient) -> None:
    owner = await account(http, "amy.reference@example.com")

    body = await settings(http, owner)

    assert (body["reference_group"], body["micronutrient_references"]) == (None, {})


async def test_choosing_a_group_returns_its_ranges(http: httpx.AsyncClient) -> None:
    owner = await account(http, "ben.reference@example.com")

    women = await settings(http, owner, reference_group="women_31_50")
    men = await settings(http, owner, reference_group="men_71_plus")

    assert women["reference_group"] == "women_31_50"
    assert women["micronutrient_references"]["iron_mg"] == {"min": 18, "max": 45}
    # An upper limit that applies only to supplements is not shown.
    assert women["micronutrient_references"]["magnesium_mg"] == {"min": 320, "max": None}
    assert men["micronutrient_references"]["iron_mg"] == {"min": 8, "max": 45}
    assert men["micronutrient_references"]["vitamin_d_ug"] == {"min": 20, "max": 100}
    assert len(men["micronutrient_references"]) == len(MICRONUTRIENTS)


@pytest.mark.parametrize(
    ("old", "new"), [("adult_women", "women_31_50"), ("adult_men", "men_31_50")]
)
async def test_the_earlier_group_names_still_work(
    http: httpx.AsyncClient, old: str, new: str
) -> None:
    # Apps from before #34 send the old names.
    owner = await account(http, f"fay.{old}@example.com")

    body = await settings(http, owner, reference_group=old)

    assert body["reference_group"] == new
    assert body["micronutrient_references"]["iron_mg"]["min"] == (18 if new == "women_31_50" else 8)


async def test_null_turns_the_reference_values_off_and_others_stay(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "cat.reference@example.com")
    await settings(http, owner, reference_group="pregnancy_19_30", weight_unit="lb")

    body = await settings(http, owner, reference_group=None)
    unchanged = await settings(http, owner, energy_unit="kj")

    assert (body["reference_group"], body["micronutrient_references"]) == (None, {})
    assert body["weight_unit"] == "lb"
    assert unchanged["reference_group"] is None


async def test_unknown_groups_are_refused_and_users_are_separate(
    http: httpx.AsyncClient,
) -> None:
    one = await account(http, "dee.reference@example.com")
    two = await account(http, "eli.reference@example.com")
    await settings(http, one, reference_group="breastfeeding_31_50")

    refused = await http.patch(
        "/api/v1/me/settings", json={"reference_group": "teens"}, headers=one.headers
    )

    assert refused.status_code == 422
    assert (await settings(http, two))["reference_group"] is None
    export = await http.get("/api/v1/export", params={"format": "json"}, headers=one.headers)
    assert export.json()["settings"]["reference_group"] == "breastfeeding_31_50"
