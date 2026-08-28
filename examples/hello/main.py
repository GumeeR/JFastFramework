"""Smallest possible JFast service.

Run it:

    pip install -e "../..[server,metrics,dev]"
    uvicorn main:app --reload

Then:

    curl localhost:8000/health
    curl localhost:8000/ready
    curl localhost:8000/info
    curl localhost:8000/metrics
"""

from fastapi import APIRouter

from jfastframework import NotFoundError, create_app

router = APIRouter()

_GREETINGS = {"en": "hello", "es": "hola"}


@router.get("/greet/{lang}", tags=["demo"])
async def greet(lang: str) -> dict[str, str]:
    try:
        return {"greeting": _GREETINGS[lang]}
    except KeyError:
        # Serialises as application/problem+json, with the request id attached.
        raise NotFoundError(f"No greeting for language {lang!r}") from None


app = create_app(routers=[router])
