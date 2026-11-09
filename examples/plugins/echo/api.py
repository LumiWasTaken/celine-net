from fastapi import APIRouter

from celine import plugins

router = APIRouter()


@router.get("/heard")
async def heard() -> dict:
    file = plugins.get("echo").db
    count = await file.read(lambda c: c.execute("SELECT count(*) FROM heard").fetchone()[0])
    return {"count": count}


async def check(services, plugin, input) -> dict:
    """The form's Check: whether a token is saved, never the token."""
    from celine.settings import form
    token = form.read_secret(services.cfg, "echo", "token")
    return {"ok": token is not None, "sentence": "A token is saved." if token else "No token."}
