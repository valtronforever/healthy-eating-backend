from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import false as sa_false
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from healthy_eating.db.base import Base, TimestampMixin
from healthy_eating.nutrition.micronutrients import (
    ITEM_MATCH_STATUSES,
    REFERENCE_DATASETS,
    REFERENCE_GROUPS,
)
from healthy_eating.nutrition.types import (
    CONFIDENCE_VALUES,
    DAY_STATUSES,
    ENERGY_UNITS,
    MEAL_SOURCES,
    NUTRIENT_FIELDS,
    OPTIONAL_NUTRIENT_FIELDS,
    STATUS_SETTERS,
    TRACKED_NUTRIENT_FIELDS,
    WEIGHT_SOURCES,
    WEIGHT_UNITS,
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({_quoted(values)})"


def _target_range(field: str) -> str:
    low, high = f"target_{field}_min", f"target_{field}_max"
    return (
        f"({low} IS NULL OR {low} >= 0) AND ({high} IS NULL OR {high} >= 0) "
        f"AND ({low} IS NULL OR {high} IS NULL OR {low} <= {high})"
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_one_of("energy_unit", ENERGY_UNITS), name="energy_unit"),
        CheckConstraint(_one_of("weight_unit", WEIGHT_UNITS), name="weight_unit"),
        CheckConstraint(_one_of("reference_group", REFERENCE_GROUPS), name="reference_group"),
        *(
            CheckConstraint(_target_range(field), name=f"target_{field}")
            for field in NUTRIENT_FIELDS
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str | None] = mapped_column(String(255))
    primary_email: Mapped[str | None] = mapped_column(String(320))

    # Settings. IANA time zone name (e.g. "Europe/Berlin"); NULL until the user or the app
    # sets it. Local days are computed in it (see services/local_time.py).
    timezone: Mapped[str | None] = mapped_column(String(64))
    energy_unit: Mapped[str] = mapped_column(String(4), default="kcal", server_default="kcal")
    calm_mode: Mapped[bool] = mapped_column(default=False, server_default=sa_false())
    # Weight is opt-in: off until the first weigh-in from the chat or the app, or until the user
    # turns it on. Off hides weight; the entries stay until the user deletes them.
    weight_tracking: Mapped[bool] = mapped_column(default=False, server_default=sa_false())
    weight_unit: Mapped[str] = mapped_column(String(2), default="kg", server_default="kg")
    # Reference values for vitamins and minerals (nutrition/micronutrients.py); NULL = off.
    reference_group: Mapped[str | None] = mapped_column(String(24))
    # Daily target ranges in canonical units; either bound may be open (NULL).
    target_calories_kcal_min: Mapped[float | None]
    target_calories_kcal_max: Mapped[float | None]
    target_protein_g_min: Mapped[float | None]
    target_protein_g_max: Mapped[float | None]
    target_fat_g_min: Mapped[float | None]
    target_fat_g_max: Mapped[float | None]
    target_carbohydrates_g_min: Mapped[float | None]
    target_carbohydrates_g_max: Mapped[float | None]

    identities: Mapped[list[Identity]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class Identity(TimestampMixin, Base):
    """A social login linked to a user. Keyed by (provider, subject), never by email."""

    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(back_populates="identities")


class OAuthClient(TimestampMixin, Base):
    """A client registered via Dynamic Client Registration (MCP clients, the iOS app)."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    client_info: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LoginRequest(Base):
    """An /authorize request waiting for the user to sign in on the login page."""

    __tablename__ = "login_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    redirect_uri: Mapped[str] = mapped_column(Text)
    redirect_uri_provided_explicitly: Mapped[bool]
    code_challenge: Mapped[str] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]


class Grant(TimestampMixin, Base):
    """One sign-in of a client (an AI assistant or the iOS app) for a user.

    Its access and refresh tokens share the grant id; revoking the grant ends them all.
    """

    __tablename__ = "grants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    # Updated at most once per minute when an access token of the grant is used.
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class LinkRequest(Base):
    """A signed-in user adding another social login to their account (services/linking.py).

    Created by the app with a PKCE challenge; the browser step stores the new identity here and
    sends a one-time code back to the app; the app finishes it with the code and its verifier.
    """

    __tablename__ = "link_requests"

    # Random and unguessable; it is in the link page's URL.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_challenge: Mapped[str] = mapped_column(String(128))
    completion_code_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider: Mapped[str | None] = mapped_column(String(32))
    subject: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime]


class AuthorizationCodeRecord(Base):
    __tablename__ = "authorization_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    redirect_uri_provided_explicitly: Mapped[bool]
    code_challenge: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]


class AccessTokenRecord(Base):
    __tablename__ = "access_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class RefreshTokenRecord(Base):
    __tablename__ = "refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]
    # Set when the token is rotated or revoked. Presenting it again revokes the whole grant.
    revoked_at: Mapped[datetime | None]


class Meal(TimestampMixin, Base):
    """A meal the user ate, with the nutrition values of the whole portion.

    Values are stored in canonical units (kcal, g). Corrections update the row in place,
    so a meal is never counted twice.
    """

    __tablename__ = "meals"
    __table_args__ = (
        CheckConstraint(_one_of("source", MEAL_SOURCES), name="source"),
        CheckConstraint(_one_of("confidence", CONFIDENCE_VALUES), name="confidence"),
        CheckConstraint(
            " AND ".join(f"{name} >= 0" for name in NUTRIENT_FIELDS), name="non_negative"
        ),
        CheckConstraint(
            " AND ".join(f"({name} IS NULL OR {name} >= 0)" for name in OPTIONAL_NUTRIENT_FIELDS),
            name="optional_non_negative",
        ),
        CheckConstraint("portion_grams IS NULL OR portion_grams > 0", name="portion_positive"),
        CheckConstraint(
            f"estimated_fields <@ ARRAY[{_quoted(TRACKED_NUTRIENT_FIELDS)}]::text[]",
            name="estimated_fields",
        ),
        Index("ix_meals_user_id_eaten_at", "user_id", "eaten_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    dish_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    source_note: Mapped[str | None] = mapped_column(String(200))
    eaten_at: Mapped[datetime]
    # UTC offset of the local time of eaten_at, to group meals by local day: the offset the
    # user gave, or the one of their time zone at that moment. NULL when neither was known.
    utc_offset_minutes: Mapped[int | None]
    portion_grams: Mapped[float | None]
    calories_kcal: Mapped[float]
    protein_g: Mapped[float]
    fat_g: Mapped[float]
    carbohydrates_g: Mapped[float]
    # Optional; NULL when the meal has no value for them.
    fiber_g: Mapped[float | None]
    sugar_g: Mapped[float | None]
    sodium_mg: Mapped[float | None]
    # Values the assistant estimated, as opposed to values from the user's own workflow.
    estimated_fields: Mapped[list[str]] = mapped_column(ARRAY(Text))
    confidence: Mapped[str] = mapped_column(String(8))
    # The OAuth client (AI assistant) that saved the meal; corrections keep it.
    client_id: Mapped[str | None] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="SET NULL")
    )
    # Set for meals imported from the user's earlier tracking (services/meal_import.py), e.g.
    # "ChatGPT project" or "Google Sheets"; NULL for meals logged as they were eaten.
    imported_from: Mapped[str | None] = mapped_column(String(100))
    # Vitamins and minerals of the matched food items (nutrition/micronutrients.py codes); NULL
    # when the meal has no items, {} when none of its items matched the reference data.
    micronutrients: Mapped[dict[str, float] | None] = mapped_column(JSONB)

    items: Mapped[list[MealItem]] = relationship(
        order_by="MealItem.position",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MealItem(Base):
    """A food of a meal with its weight, and the reference food it matched (#25).

    The matched food is a snapshot (id and description): reference data is replaced when a new
    version is bundled, so there is no foreign key to it.
    """

    __tablename__ = "meal_items"
    __table_args__ = (
        CheckConstraint(_one_of("status", ITEM_MATCH_STATUSES), name="status"),
        CheckConstraint("grams IS NULL OR grams > 0", name="grams_positive"),
        # Only a label item may have no weight (a capsule); reference foods scale with grams.
        CheckConstraint("status = 'label' OR grams IS NOT NULL", name="grams_unless_label"),
    )

    meal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meals.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200))
    # NULL only for label items without a weight.
    grams: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(8))
    # The USDA food (fdc_id) and its description, only for `matched` items.
    food_id: Mapped[int | None]
    food_description: Mapped[str | None] = mapped_column(Text)
    # Vitamins and minerals from the item's label or the user (status `label`), for the amount
    # taken, in the units of the codes; never estimated.
    label_values: Mapped[dict[str, float] | None] = mapped_column(JSONB)


class WeightEntry(TimestampMixin, Base):
    """One weigh-in, in kg. Corrections update the row; Apple Health samples keep their id."""

    __tablename__ = "weights"
    __table_args__ = (
        CheckConstraint(_one_of("source", WEIGHT_SOURCES), name="source"),
        CheckConstraint("weight_kg > 0", name="weight_positive"),
        Index("ix_weights_user_id_measured_at", "user_id", "measured_at"),
        # An Apple Health sample is imported once per user, however often the app sends it.
        Index(
            "uq_weights_user_id_external_id",
            "user_id",
            "external_id",
            unique=True,
            postgresql_where="external_id IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    weight_kg: Mapped[float]
    # Stored in UTC; utc_offset_minutes keeps the local offset, as for meals.
    measured_at: Mapped[datetime]
    utc_offset_minutes: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str | None] = mapped_column(String(255))


class WidgetToken(Base):
    """A read-only token for the iOS widgets (`/api/v1/widget/today` only), one per app sign-in.

    Stored as a SHA-256 hash. It ends with its grant: revoking or deleting the app's sign-in
    deletes or disables it, and deleting the account deletes it.
    """

    __tablename__ = "widget_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grants.id", ondelete="CASCADE"), unique=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None]


class DayStatusRecord(TimestampMixin, Base):
    """Whether the user logged every meal of a local day (see services/days.py)."""

    __tablename__ = "day_statuses"
    __table_args__ = (
        CheckConstraint(_one_of("status", DAY_STATUSES), name="status"),
        CheckConstraint(_one_of("set_by", STATUS_SETTERS), name="set_by"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    local_date: Mapped[date] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(8))
    set_by: Mapped[str] = mapped_column(String(9))
    # Set by an import of earlier history: such a mark makes the day count, but does not switch
    # the user to counting complete days only (services/days.py).
    from_import: Mapped[bool] = mapped_column(default=False, server_default=sa_false())


class ReferenceData(Base):
    """Which version of a bundled reference dataset is loaded (services/reference_data.py)."""

    __tablename__ = "reference_data"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    # Release of each part, e.g. {"fndds": "2024-10-31", "sr_legacy": "2018-04"}.
    datasets: Mapped[dict[str, Any]] = mapped_column(JSONB)
    foods: Mapped[int]
    loaded_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReferenceFood(Base):
    """A food of open reference data (USDA FoodData Central, CC0), values per 100 g.

    Read-only public data, replaced as a whole when the bundled file changes; never user data.
    """

    __tablename__ = "reference_foods"
    __table_args__ = (
        CheckConstraint(_one_of("dataset", REFERENCE_DATASETS), name="dataset"),
        Index("ix_reference_foods_search", "search", postgresql_using="gin"),
    )

    # The FoodData Central id (fdc_id).
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    dataset: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text)
    energy_kcal: Mapped[float | None]
    # Micronutrient code (nutrition/micronutrients.py) -> amount per 100 g; missing when unknown.
    nutrients: Mapped[dict[str, float]] = mapped_column(JSONB)
    search: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', description)", persisted=True)
    )
