import uuid

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token


def current_user_id() -> uuid.UUID:
    """The id of the user who authorized the MCP client making this request."""
    token = get_access_token()
    if token is None or token.subject is None:
        raise ToolError("Not authenticated")
    return uuid.UUID(token.subject)
