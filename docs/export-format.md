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

- Energy in kcal; protein, fat, carbohydrates and portions in grams; body weight in kg. Users
  who chose kJ or lb in the app still get kcal and kg here.
- Times are ISO 8601 with a UTC offset. `eaten_at_utc` is the stored time; `eaten_at_local`
  and `local_date` are the meal's local time and the local day it counts for (its own UTC
  offset if known, otherwise the user's time zone, otherwise UTC).

## JSON

```json
{
 "format_version": 1,
 "exported_at": "2026-10-19T08:00:00+00:00",
 "units": {"energy": "kcal", "macronutrients": "g", "portion": "g", "weight": "kg"},
 "account": {"id": "…", "display_name": "…", "email": "…", "created_at": "…"},
 "logins": [{"provider": "google", "email": "…", "email_verified": true,
             "created_at": "…", "last_login_at": "…"}],
 "settings": {"timezone": "Europe/Berlin", "energy_unit": "kcal", "calm_mode": false,
              "weight_tracking": true, "weight_unit": "kg", "reference_group": null,
              "targets": {"calories_kcal": {"min": 1800, "max": 2200}, "protein_g": null,
                          "fat_g": null, "carbohydrates_g": null}},
 "connections": [{"client_name": "Claude", "kind": "ai_assistant", "created_at": "…",
                  "last_used_at": "…", "revoked_at": null}],
 "day_statuses": [{"date": "2026-10-18", "status": "complete", "set_by": "user",
                   "updated_at": "…"}],
 "weights": [{"weight_id": "…", "weight_kg": 78.4, "…": "…"}],
 "meals": [{"meal_id": "…", "dish_name": "…", "…": "…"}]
}
```

- `connections`: every sign-in ever made, revoked ones too (`revoked_at` set). `kind` is
  `ai_assistant` (an MCP client) or `ios_app`.
- `day_statuses`: days marked `complete` or `partial`; `set_by` is `user` or `assistant`;
  `from_import` is true for marks an import of earlier history set (they make the day count
  but do not switch averages to complete days only).
- `weights`: weigh-ins, oldest first: `weight_id`, `weight_kg`, `measured_at_utc`,
  `measured_at_local`, `local_date`, `utc_offset_minutes`, `source` (`app`, `assistant` or
  `apple_health`), `external_id` (the Apple Health sample id, only for `apple_health`),
  `created_at`, `updated_at`. Present even when weight tracking is off.
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
| `imported_from` | For meals imported from earlier tracking: where they came from, e.g. "ChatGPT project"; empty/null otherwise |
| `micronutrients` | Vitamins and minerals of the meal's matched food items (from USDA FoodData Central) and label items (from their label): an object of `vitamin_a_ug`, `vitamin_c_mg`, ... `selenium_ug` (the unit is the suffix); null when the meal has no items, `{}` when none has values; a nutrient no item has a value for is left out. In CSV: JSON text |
| `items` | The meal's food items: `name`, `grams` (null for a label item without a weight), `status` (`matched`, `weak`, `none`, or `label` for a supplement or packaged food whose values come from its label), `food_id` and `food_description` of the USDA food used (only when matched), `label_values` (the label's values keyed like `micronutrients`, only for `label` items, otherwise null); `[]` when there are none. In CSV: JSON text (empty when there are none) |
| `estimated_fields` | Values the assistant estimated (JSON list; in CSV joined with `;`) |
| `confidence` | `high`, `medium` or `low` |
| `client_name` | The AI assistant that first saved the meal, if known |
| `created_at`, `updated_at` | When the meal was saved and last changed |

## CSV (ZIP)

| File | Content |
|---|---|
| `account.json` | `format_version`, `exported_at` and the `account` object as above |
| `logins.csv` | `provider, email, email_verified, created_at, last_login_at` |
| `settings.csv` | `setting, value`: `timezone`, `energy_unit`, `calm_mode`, `weight_tracking`, `weight_unit`, `reference_group`, `target_<nutrient>_<min\|max>` |
| `connections.csv` | `client_name, kind, created_at, last_used_at, revoked_at` |
| `day_statuses.csv` | `date, status, set_by, from_import, updated_at` |
| `weights.csv` | The weigh-in fields above, in that order |
| `meals.csv` | The meal fields above, in that order |

CSV files are UTF-8 with a header row. Empty cells mean "not known" or "not set"; booleans are
`true`/`false`; numbers use a dot as the decimal separator.

## Importing an export

`POST /api/v1/import` imports an export into the signed-in account, on this or another server,
for example to move to a self-hosted server. The request body is the export file itself
(`Content-Type: application/json` for the JSON file, `application/zip` for the ZIP), at most
50 MB; the format is recognized from the content. Format versions up to this server's are read.

- **What is imported:** meals (all fields above, with their items; vitamins and minerals are
  computed again from the items), weigh-ins and the marks of the days that get meals. The
  account, logins, settings and connections stay as they are on the receiving server. Meals and
  weigh-ins keep their ids unless another account on the server already uses them. Imported
  weigh-ins turn weight tracking on.
- **Nothing twice:** a meal whose id the account already has, or with the same name (case and
  spacing ignored), minute and calories, is skipped; so is a weigh-in with a known id, Apple
  Health sample or the same time and weight. Importing the same file again changes nothing.
- **Day marks:** a day that gets meals takes the export's mark; without one it is marked
  complete, or partial when the exported account counted complete days only (its unmarked days
  did not count there). Such marks have `from_import` set: they let the day count but do not
  switch the account to counting complete days only. A day the account already marked keeps its
  mark.
- **Invalid entries** (a missing or out-of-range value, an unknown source, a meal in the future)
  are left out; the answer counts them and names the first 50 with their position and reason.
- `?dry_run=true` checks the file and returns the same answer without storing anything, so an
  app can show a preview first.

The answer (`ImportResponse` in `openapi.json`) gives the file's format and export time, the
imported and skipped meals and weigh-ins, the marked days and the first and last day of the
imported meals. A file that is not an export is refused with 400, a larger one with 413.
