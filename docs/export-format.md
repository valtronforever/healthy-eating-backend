# Data export format

`GET /api/v1/export` downloads everything Healthy Eating stores about the signed-in user.
It needs the same bearer token as the rest of the API. Tokens, secrets and other users' data
are never included.

| Query | Result |
|---|---|
| `format=json` (default) | One JSON file, `healthy-eating-export-<date>.json` |
| `format=csv` | A ZIP file, `healthy-eating-export-<date>.zip`, with one CSV file per table |

The export is streamed, so large histories download without the server holding them in
memory. Format version: **1**. Fields may be added in later versions; existing fields keep
their meaning.

## Units and times

- Energy in kcal; protein, fat, carbohydrates and portions in grams. Users who chose kJ in
  the app still get kcal here.
- Times are ISO 8601 with a UTC offset. `eaten_at_utc` is the stored time; `eaten_at_local`
  and `local_date` are the meal's local time and the local day it counts for (its own UTC
  offset if known, otherwise the user's time zone, otherwise UTC).

## JSON

```json
{
 "format_version": 1,
 "exported_at": "2026-10-19T08:00:00+00:00",
 "units": {"energy": "kcal", "macronutrients": "g", "portion": "g"},
 "account": {"id": "…", "display_name": "…", "email": "…", "created_at": "…"},
 "logins": [{"provider": "google", "email": "…", "email_verified": true,
             "created_at": "…", "last_login_at": "…"}],
 "settings": {"timezone": "Europe/Berlin", "energy_unit": "kcal", "calm_mode": false,
              "targets": {"calories_kcal": {"min": 1800, "max": 2200}, "protein_g": null,
                          "fat_g": null, "carbohydrates_g": null}},
 "connections": [{"client_name": "Claude", "kind": "ai_assistant", "created_at": "…",
                  "last_used_at": "…", "revoked_at": null}],
 "day_statuses": [{"date": "2026-10-18", "status": "complete", "set_by": "user",
                   "updated_at": "…"}],
 "meals": [{"meal_id": "…", "dish_name": "…", "…": "…"}]
}
```

- `connections`: every sign-in ever made, revoked ones too (`revoked_at` set). `kind` is
  `ai_assistant` (an MCP client) or `ios_app`.
- `day_statuses`: days marked `complete` or `partial`; `set_by` is `user` or `assistant`.
- `meals`: oldest first, with the fields below.

## Meal fields

| Field | Meaning |
|---|---|
| `meal_id` | Stable id; the same as in the API and MCP tools |
| `dish_name`, `description` | As saved by the assistant or edited in the app |
| `eaten_at_utc`, `eaten_at_local`, `local_date` | See "Units and times" |
| `utc_offset_minutes` | Offset of the local time, empty/null when it was not known |
| `portion_grams` | Portion weight, if known |
| `calories_kcal`, `protein_g`, `fat_g`, `carbohydrates_g` | Values for the whole portion |
| `fiber_g`, `sugar_g`, `sodium_mg` | Optional values for the whole portion (sodium in mg); empty when not known |
| `source` | `from_conversation` (numbers from the user's own workflow) or `estimated_by_assistant` |
| `source_note` | Where existing numbers came from, e.g. "Google Sheets log" |
| `estimated_fields` | Values the assistant estimated (JSON list; in CSV joined with `;`) |
| `confidence` | `high`, `medium` or `low` |
| `client_name` | The AI assistant that first saved the meal, if known |
| `created_at`, `updated_at` | When the meal was saved and last changed |

## CSV (ZIP)

| File | Content |
|---|---|
| `account.json` | `format_version`, `exported_at` and the `account` object as above |
| `logins.csv` | `provider, email, email_verified, created_at, last_login_at` |
| `settings.csv` | `setting, value`: `timezone`, `energy_unit`, `calm_mode`, `target_<nutrient>_<min\|max>` |
| `connections.csv` | `client_name, kind, created_at, last_used_at, revoked_at` |
| `day_statuses.csv` | `date, status, set_by, updated_at` |
| `meals.csv` | The meal fields above, in that order |

CSV files are UTF-8 with a header row. Empty cells mean "not known" or "not set"; booleans are
`true`/`false`; numbers use a dot as the decimal separator.
