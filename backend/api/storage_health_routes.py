"""SQLite-independent storage health diagnostics."""
from fastapi import APIRouter, Depends

from application.dependencies import get_application
from application.service import ApplicationService

router = APIRouter()


@router.get("/storage/health")
async def storage_health(application: ApplicationService = Depends(get_application)):
    """Return fresh canonical storage state without opening the database."""
    return await application.storage_health()
