"""Reserved voice endpoints (ARCHITECTURE 17.4).

MVP is text-only. These paths return 501 deliberately: publishing the contract now
means V2 adds voice without a breaking API change.
"""

from fastapi import APIRouter, status

router = APIRouter(prefix="/widget", tags=["voice"])


@router.post(
    "/voice-sessions",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    responses={501: {"description": "Voice is not available in this version"}},
)
def create_voice_session() -> dict[str, str]:
    return {"error": "voice is not available in this version"}
