"""Role-based access control (R13 / AUTH-04).

Roles are ordered: owner > admin > viewer. Enforcement is server-side only; the
dashboard is never trusted to hide a button.
"""

from enum import StrEnum

from fastapi import HTTPException, status


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    VIEWER = "viewer"


# Higher number = more privilege.
_RANK = {Role.VIEWER: 1, Role.ADMIN: 2, Role.OWNER: 3}


def at_least(role: str, minimum: Role) -> bool:
    try:
        return _RANK[Role(role)] >= _RANK[minimum]
    except ValueError:
        # Unknown role string: deny rather than guess.
        return False


def require(role: str, minimum: Role) -> None:
    """Raise 403 unless ``role`` meets ``minimum``."""
    if not at_least(role, minimum):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": f"requires role {minimum.value} or higher",
                }
            },
        )
