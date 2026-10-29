from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.mcp_server.tools.lab_results import GOALS_NOTE
from healthy_eating.nutrition.lab_analytes import (
    ANALYTE_INFO,
    LAB_ANALYTES,
    conversion,
    to_canonical,
    unit_key,
)
from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account

REPORT: dict[str, Any] = {
    "taken_on": "2026-10-20",
    "results": [
        {
            "name": "25-OH-Vitamin D",
            "analyte": "vitamin_d_25oh",
            "value": 18,
            "unit": "ng/mL",
            "reference_low": 30,
            "reference_high": 100,
        },
        {"name": "Ferritin", "analyte": "ferritin", "value": 22, "unit": "µg/l"},
        {"name": "Hämoglobin", "analyte": "hemoglobin", "value": 13.1, "unit": "g/dL"},
        {"name": "CRP", "value": 0.5, "unit": "mg/dL", "comparator": "<"},
    ],
}


async def call(
    app: FastAPI, owner: Account, tool: str, arguments: dict[str, Any] | None = None
) -> Any:
    async with mcp_client(app, owner.token) as mcp:
        return await mcp.call_tool(tool, arguments or {}, raise_on_error=False)


async def saved(app: FastAPI, owner: Account, report: dict[str, Any]) -> dict[str, Any]:
    result = await call(app, owner, "log_lab_results", report)
    assert not result.is_error, result.content
    content: dict[str, Any] = result.structured_content
    return content


async def reports(http: httpx.AsyncClient, owner: Account, **params: Any) -> list[dict[str, Any]]:
    response = await http.get("/api/v1/lab-results", params=params, headers=owner.headers)
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()["reports"]
    return found


def test_every_canonical_unit_converts_to_itself() -> None:
    for analyte in LAB_ANALYTES:
        info = ANALYTE_INFO[analyte]
        how = conversion(analyte, info.canonical_unit)
        assert how is not None, analyte
        assert (how.factor, how.offset) == (1, 0), analyte


@pytest.mark.parametrize(
    ("analyte", "value", "unit", "canonical"),
    [
        ("vitamin_d_25oh", 30, "ng/mL", 74.88),
        ("vitamin_d_25oh", 75, "nmol/L", 75),
        ("vitamin_b12", 400, "pg/ml", 295.12),
        ("ferritin", 22, "ng/mL", 22),
        ("iron", 100, "µg/dL", 17.91),
        ("hemoglobin", 13.5, "g/dL", 135),
        ("magnesium", 2.0, "mg/dL", 0.823),
        ("selenium", 1.2, "μmol/L", 94.764),
        ("cholesterol_total", 200, "mg/dL", 5.172),
        ("glucose", 90, "mg/dl", 4.996),
        # The IFCC-NGSP master equation: 48 mmol/mol is 6.5 %.
        ("hba1c", 48, "mmol/mol", 6.543),
        ("tsh", 2.1, "µIU/mL", 2.1),
    ],
)
def test_units_convert_to_the_canonical_unit(
    analyte: Any, value: float, unit: str, canonical: float
) -> None:
    how = conversion(analyte, unit)
    assert how is not None
    assert to_canonical(value, how) == pytest.approx(canonical, abs=0.001)


def test_units_are_compared_without_case_spaces_or_the_spelling_of_micro() -> None:
    assert unit_key("µg / L") == unit_key("μg/l") == unit_key("mcg/L") == "ug/l"
    assert conversion("zinc", "mg/L") is None
    assert conversion("ferritin", None) is None


async def test_a_report_is_saved_as_printed_and_converted(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "lea@example.com", "Europe/Berlin")

    content = await saved(app, owner, REPORT)

    assert content["saved_to_healthy_eating"] is True
    assert content["user_message"] == "Saved 4 lab results from 2026-10-20 to HealthyEating."
    assert content["related_nutrients"] == ["vitamin_d_ug", "iron_mg"]
    assert content["goals_note"] == GOALS_NOTE
    vitamin_d, ferritin, hemoglobin, crp = content["results"]
    assert (vitamin_d["value"], vitamin_d["unit"]) == (18, "ng/mL")
    assert (vitamin_d["canonical_value"], vitamin_d["canonical_unit"]) == (44.928, "nmol/L")
    assert (ferritin["canonical_value"], ferritin["canonical_unit"]) == (22, "µg/L")
    assert hemoglobin["canonical_value"] == 131
    # Anything else is kept as printed, without a canonical value.
    assert crp["analyte"] is None
    assert (crp["comparator"], crp["canonical_value"]) == ("<", None)

    [report] = await reports(http, owner)
    assert report["report_id"] == content["report_id"]
    assert report["taken_on"] == "2026-10-20"
    first = report["results"][0]
    assert first["name"] == "25-OH-Vitamin D"
    assert (first["reference_low"], first["reference_high"]) == (30, 100)
    assert first["canonical"] == {
        "value": 44.928,
        "unit": "nmol/L",
        "reference_low": 74.88,
        "reference_high": 249.6,
    }
    assert first["related_nutrient"] == "vitamin_d_ug"
    assert [r["name"] for r in report["results"]] == [
        "25-OH-Vitamin D",
        "Ferritin",
        "Hämoglobin",
        "CRP",
    ]


async def test_results_without_a_related_nutrient_have_no_goals_note(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "max@example.com")
    lipids = {
        "taken_on": "2026-09-01",
        "results": [{"name": "LDL", "analyte": "ldl_cholesterol", "value": 3.1, "unit": "mmol/L"}],
    }

    content = await saved(app, owner, lipids)

    assert content["user_message"] == "Saved 1 lab result from 2026-09-01 to HealthyEating."
    assert content["related_nutrients"] == []
    assert content["goals_note"] is None


async def test_a_correction_replaces_the_report(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "noa@example.com")
    first = await saved(app, owner, REPORT)
    corrected = {
        "taken_on": "2026-10-21",
        "results": [REPORT["results"][0] | {"value": 19}],
        "replaces_report_id": first["report_id"],
    }

    content = await saved(app, owner, corrected)

    assert content["report_id"] == first["report_id"]
    assert content["user_message"] == (
        "Updated the lab report from 2026-10-21 in HealthyEating: 1 lab result."
    )
    [report] = await reports(http, owner)
    assert report["taken_on"] == "2026-10-21"
    assert [r["value"] for r in report["results"]] == [19]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"taken_on": "2099-01-01"}, "in the future"),
        ({"taken_on": "1899-12-31"}, "before 1900"),
        (
            {"results": [{"name": "Zinc", "value": 12, "reference_low": 10, "reference_high": 9}]},
            "reference_low is above reference_high",
        ),
        ({"results": [{"name": "   ", "value": 1}]}, "the name is empty"),
    ],
)
async def test_invalid_reports_are_not_saved(
    app: FastAPI, http: httpx.AsyncClient, change: dict[str, Any], reason: str
) -> None:
    owner = await account(http, "ola@example.com")

    result = await call(app, owner, "log_lab_results", REPORT | change)

    assert result.is_error
    assert "Not saved to HealthyEating" in result.content[0].text
    assert reason in result.content[0].text
    assert await reports(http, owner) == []


async def test_users_only_see_and_change_their_own_lab_results(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "pia@example.com")
    other = await account(http, "quin@example.com")
    report = await saved(app, owner, REPORT)
    result_id = report["results"][0]["result_id"]

    assert await reports(http, other) == []
    listed = (await call(app, other, "list_lab_results")).structured_content
    assert listed["reports"] == []
    replace = await call(
        app, other, "log_lab_results", REPORT | {"replaces_report_id": report["report_id"]}
    )
    assert replace.is_error
    assert "was not found" in replace.content[0].text
    for path in (f"reports/{report['report_id']}", result_id):
        response = await http.delete(f"/api/v1/lab-results/{path}", headers=other.headers)
        assert response.status_code == 404
    deleted = await call(app, other, "delete_lab_results", {"report_id": report["report_id"]})
    assert deleted.is_error
    assert len((await reports(http, owner))[0]["results"]) == 4


async def test_lists_filter_by_day_and_analyte(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "rae@example.com")
    await saved(app, owner, REPORT)
    await saved(
        app,
        owner,
        {
            "taken_on": "2026-04-02",
            "results": [{"name": "Ferritin", "analyte": "ferritin", "value": 15, "unit": "ng/mL"}],
        },
    )

    assert [r["taken_on"] for r in await reports(http, owner)] == ["2026-10-20", "2026-04-02"]
    ferritin = await reports(http, owner, analyte="ferritin")
    assert [[r["value"] for r in report["results"]] for report in ferritin] == [[22], [15]]
    spring = await reports(http, owner, **{"from": "2026-01-01", "to": "2026-06-30"})
    assert [r["taken_on"] for r in spring] == ["2026-04-02"]
    response = await http.get(
        "/api/v1/lab-results",
        params={"from": "2026-06-30", "to": "2026-01-01"},
        headers=owner.headers,
    )
    assert response.status_code == 400

    listed = (
        await call(app, owner, "list_lab_results", {"analytes": ["vitamin_d_25oh"]})
    ).structured_content
    [report] = listed["reports"]
    assert [r["name"] for r in report["results"]] == ["25-OH-Vitamin D"]


async def test_deleting_one_result_or_a_whole_report(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "sam@example.com")
    first = await saved(app, owner, REPORT)
    second = await saved(app, owner, REPORT | {"taken_on": "2026-10-01"})

    response = await http.delete(
        f"/api/v1/lab-results/{first['results'][3]['result_id']}", headers=owner.headers
    )
    assert response.status_code == 204
    response = await http.delete(
        f"/api/v1/lab-results/reports/{second['report_id']}", headers=owner.headers
    )
    assert response.status_code == 204
    [report] = await reports(http, owner)
    assert len(report["results"]) == 3

    both = await call(
        app,
        owner,
        "delete_lab_results",
        {"report_id": first["report_id"], "result_id": first["results"][0]["result_id"]},
    )
    assert both.is_error
    deleted = await call(app, owner, "delete_lab_results", {"report_id": first["report_id"]})
    assert deleted.structured_content == {
        "deleted_from_healthy_eating": True,
        "user_message": "Deleted the lab report (3 lab results) from HealthyEating.",
    }
    assert await reports(http, owner) == []
