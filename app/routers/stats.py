from fastapi import APIRouter
from app.services.stats_service import stats_service

router = APIRouter()

@router.get("/stats")
async def get_stats():
    """
    Returns real-time performance metrics and provider health status.
    """
    return stats_service.get_stats()
