from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from healthy_eating.db.base import Base, TimestampMixin
from healthy_eating.nutrition.types import CONFIDENCE_VALUES, MEAL_SOURCES, NUTRIENT_FIELDS


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str | None] = mapped_column(String(255))
    primary_email: Mapped[str | None] = mapped_column(String(320))

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
        CheckConstraint("portion_grams IS NULL OR portion_grams > 0", name="portion_positive"),
        CheckConstraint(
            f"estimated_fields <@ ARRAY[{', '.join(repr(v) for v in NUTRIENT_FIELDS)}]::text[]",
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
    # Offset of the local time the user gave for eaten_at, to group meals by local day.
    # NULL when the time was not given (eaten "just now").
    utc_offset_minutes: Mapped[int | None]
    portion_grams: Mapped[float | None]
    calories_kcal: Mapped[float]
    protein_g: Mapped[float]
    fat_g: Mapped[float]
    carbohydrates_g: Mapped[float]
    # Values the assistant estimated, as opposed to values from the user's own workflow.
    estimated_fields: Mapped[list[str]] = mapped_column(ARRAY(Text))
    confidence: Mapped[str] = mapped_column(String(8))
