"""Media-volume status for the header meter and the Settings floor field."""

from fastapi import APIRouter, HTTPException

from app.services import storage

router = APIRouter(tags=["storage"])


@router.get("/api/storage")
async def storage_status() -> dict:
    status = await storage.space_status()
    if status is None:
        raise HTTPException(503, detail="Media volume is not readable")
    return status.as_dict()
