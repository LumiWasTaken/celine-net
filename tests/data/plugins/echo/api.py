from fastapi import APIRouter

from celine import plugins

router = APIRouter()


@router.get("/heard")
async def heard() -> dict:
    file = plugins.get("echo").file
    count = await file.read(lambda c: c.execute("SELECT count(*) FROM heard").fetchone()[0])
    return {"count": count}
