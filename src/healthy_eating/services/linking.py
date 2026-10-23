"""Adding another social login to the signed-in user's account (healthy-eating-backend#20).

Accounts are never linked by email. A signed-in user links a login explicitly, in three steps
that work like an OAuth authorization code with PKCE:

1. The app asks for a link request with a PKCE challenge and opens the returned URL in the
   system web sheet.
2. The user signs in with the provider there; the new identity waits in the link request, and
   the browser goes back to the app with a one-time completion code.
3. The app finishes the link with that code and its PKCE verifier, signed in as the same user.

Someone who forwards a link URL to another person cannot finish it: the completion code goes to
the device that signed in, and only the app that asked has the verifier. A login that already
belongs to another account is refused; accounts are never merged. Unlinking keeps at least one
login, so the account can still be signed in to.
"""

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.auth.tokens import generate_token, hash_token
from healthy_eating.db.models import Identity, LinkRequest
from healthy_eating.services.users import SocialProfile

LINK_TTL = timedelta(minutes=10)


def pkce_matches(verifier: str, challenge: str) -> bool:
    """S256: the challenge is the unpadded base64url SHA-256 of the verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(expected, challenge)


async def create_link_request(
    session: AsyncSession, user_id: uuid.UUID, code_challenge: str, *, now: datetime
) -> LinkRequest:
    request = LinkRequest(
        id=generate_token(),
        user_id=user_id,
        code_challenge=code_challenge,
        expires_at=now + LINK_TTL,
    )
    session.add(request)
    await session.flush()
    return request


async def open_link_request(
    session: AsyncSession, request_id: str, *, now: datetime
) -> LinkRequest | None:
    """A link request still waiting for the browser step, or None."""
    request = await session.get(LinkRequest, request_id)
    if request is None or request.expires_at <= now or request.completion_code_hash is not None:
        return None
    return request


async def finish_browser_step(
    session: AsyncSession, request_id: str, profile: SocialProfile, *, now: datetime
) -> str | None:
    """Keep the identity the user signed in with; returns the completion code for the app."""
    request = await open_link_request(session, request_id, now=now)
    if request is None:
        return None
    code = generate_token()
    request.completion_code_hash = hash_token(code)
    request.provider = profile.provider
    request.subject = profile.subject
    request.email = profile.email
    request.email_verified = profile.email_verified
    return code


class LinkResult(StrEnum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    # The login belongs to another Healthy Eating account: never merged.
    OTHER_ACCOUNT = "other_account"
    # Unknown, expired, used or someone else's code, or a verifier that does not match.
    INVALID = "invalid"


async def finish_link(
    session: AsyncSession, user_id: uuid.UUID, code: str, code_verifier: str, *, now: datetime
) -> LinkResult:
    """Attach the waiting identity to the user. The link request is used up either way."""
    request = await session.scalar(
        select(LinkRequest).where(LinkRequest.completion_code_hash == hash_token(code))
    )
    if request is None:
        return LinkResult.INVALID
    await session.delete(request)
    valid = (
        request.user_id == user_id
        and request.expires_at > now
        and request.provider is not None
        and request.subject is not None
        and pkce_matches(code_verifier, request.code_challenge)
    )
    if not valid:
        return LinkResult.INVALID
    existing = await session.scalar(
        select(Identity).where(
            Identity.provider == request.provider, Identity.subject == request.subject
        )
    )
    if existing is not None:
        same_account = existing.user_id == user_id
        return LinkResult.ALREADY_LINKED if same_account else LinkResult.OTHER_ACCOUNT
    session.add(
        Identity(
            user_id=user_id,
            provider=request.provider,
            subject=request.subject,
            email=request.email,
            email_verified=request.email_verified,
        )
    )
    await session.flush()
    return LinkResult.LINKED


async def list_logins(session: AsyncSession, user_id: uuid.UUID) -> list[Identity]:
    return list(
        await session.scalars(
            select(Identity).where(Identity.user_id == user_id).order_by(Identity.created_at)
        )
    )


class UnlinkResult(StrEnum):
    UNLINKED = "unlinked"
    LAST_LOGIN = "last_login"
    NOT_FOUND = "not_found"


async def unlink_login(
    session: AsyncSession, user_id: uuid.UUID, identity_id: uuid.UUID
) -> UnlinkResult:
    """Remove one of the user's logins, never the last one."""
    owned = await session.scalar(
        select(Identity.id).where(Identity.id == identity_id, Identity.user_id == user_id)
    )
    if owned is None:
        return UnlinkResult.NOT_FOUND
    count = await session.scalar(
        select(func.count()).select_from(Identity).where(Identity.user_id == user_id)
    )
    if (count or 0) <= 1:
        return UnlinkResult.LAST_LOGIN
    await session.execute(delete(Identity).where(Identity.id == identity_id))
    return UnlinkResult.UNLINKED
