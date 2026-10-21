from fastapi import APIRouter

from dafeiyu import __version__

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"ok": True, "version": __version__}
