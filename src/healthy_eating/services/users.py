import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Identity, User


@dataclass(frozen=True)
class SocialProfile:
    """Normalized identity returned by a social login provider."""

    provider: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None


async def find_or_create_user(session: AsyncSession, profile: SocialProfile) -> User:
    """Return the user linked to this social identity, creating both on first login."""
    identity = await session.scalar(
        select(Identity)
        .where(Identity.provider == profile.provider, Identity.subject == profile.subject)
        .options(selectinload(Identity.user))
    )
    if identity is not None:
        identity.last_login_at = utcnow()
        if profile.email:
            identity.email = profile.email
            identity.email_verified = profile.email_verified
        user = identity.user
        if user.display_name is None and profile.display_name:
            user.display_name = profile.display_name
        return user

    user = User(display_name=profile.display_name, primary_email=profile.email)
    user.identities.append(
        Identity(
            provider=profile.provider,
            subject=profile.subject,
            email=profile.email,
            email_verified=profile.email_verified,
            last_login_at=utcnow(),
        )
    )
    session.add(user)
    await session.flush()
    return user


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.scalar(
        select(User).where(User.id == user_id).options(selectinload(User.identities))
    )


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Delete the account and, through the database's cascades, all of its data: identities,
    settings, meals, day statuses, sign-ins and tokens. False if the user does not exist."""
    deleted = await session.scalar(delete(User).where(User.id == user_id).returning(User.id))
    return deleted is not None
