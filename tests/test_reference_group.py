"""The optional reference group for vitamin and mineral reference ranges (#25)."""

from typing import Any

import httpx
import pytest

from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS, REFERENCE_INTAKES
from tests.test_meals_api import Account, account


async def settings(http: httpx.AsyncClient, owner: Account, **changes: Any) -> dict[str, Any]:
    if changes:
        response = await http.patch("/api/v1/me/settings", json=changes, headers=owner.headers)
    else:
        response = await http.get("/api/v1/me/settings", headers=owner.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize("group", ["adult_women", "adult_men"])
def test_every_group_covers_every_nutrient_with_sensible_ranges(group: str) -> None:
    references = REFERENCE_INTAKES[group]  # type: ignore[index]
    assert set(references) == set(MICRONUTRIENTS)
    for reference in references.values():
        assert reference.min > 0
        assert reference.max is None or reference.max > reference.min


async def test_reference_values_are_off_until_a_group_is_chosen(http: httpx.AsyncClient) -> None:
    owner = await account(http, "amy.reference@example.com")

    body = await settings(http, owner)

    assert (body["reference_group"], body["micronutrient_references"]) == (None, {})


async def test_choosing_a_group_returns_its_ranges(http: httpx.AsyncClient) -> None:
    owner = await account(http, "ben.reference@example.com")

    women = await settings(http, owner, reference_group="adult_women")
    men = await settings(http, owner, reference_group="adult_men")

    assert women["reference_group"] == "adult_women"
    assert women["micronutrient_references"]["iron_mg"] == {"min": 18, "max": 45}
    # An upper limit that applies only to supplements is not shown.
    assert women["micronutrient_references"]["magnesium_mg"] == {"min": 320, "max": None}
    assert men["micronutrient_references"]["iron_mg"] == {"min": 8, "max": 45}
    assert len(men["micronutrient_references"]) == len(MICRONUTRIENTS)


async def test_null_turns_the_reference_values_off_and_others_stay(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "cat.reference@example.com")
    await settings(http, owner, reference_group="adult_men", weight_unit="lb")

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
    await settings(http, one, reference_group="adult_women")

    refused = await http.patch(
        "/api/v1/me/settings", json={"reference_group": "teens"}, headers=one.headers
    )

    assert refused.status_code == 422
    assert (await settings(http, two))["reference_group"] is None
    export = await http.get("/api/v1/export", params={"format": "json"}, headers=one.headers)
    assert export.json()["settings"]["reference_group"] == "adult_women"
