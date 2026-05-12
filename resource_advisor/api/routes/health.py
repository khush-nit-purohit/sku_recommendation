from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health_check():
    return {"status": "ok"}

@router.get("/ready")
async def readiness_check():
    # To be implemented: check credentials and APIs
    return {"status": "ready"}
