import uuid

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.services.users import get_user


class Profile(BaseModel):
    user_id: uuid.UUID
    display_name: str | None = Field(description="Name shown in the app, if known")
    linked_logins: list[str] = Field(
        description="Social providers linked to this account, e.g. google, apple"
    )


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_profile() -> Profile:
        """Return the signed-in user's Healthy Eating profile."""
        async with db.session() as session:
            user = await get_user(session, current_user_id())
        if user is None:
            raise ToolError("User not found")
        return Profile(
            user_id=user.id,
            display_name=user.display_name,
            linked_logins=sorted(identity.provider for identity in user.identities),
        )
