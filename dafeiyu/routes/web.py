from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/web", include_in_schema=False)
async def dashboard():
    return FileResponse(
        Path(__file__).resolve().parent.parent / "web" / "index.html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
