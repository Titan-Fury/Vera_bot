"""FastAPI exception handlers - map errors to spec-mandated 400 / 409 JSON."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def _safe_errors(errors: list) -> list:
    """Scrub pydantic error records so JSONResponse can serialize them.

    pydantic v2 includes the raw `input` in each error; for request bodies
    that arrive malformed, `input` may be `bytes`, which the default JSON
    encoder rejects. We drop `input`/`ctx` (which can also hold arbitrary
    objects) and keep the spec-relevant fields only.
    """
    cleaned: list = []
    for e in errors:
        if isinstance(e, dict):
            loc = e.get("loc")
            cleaned.append(
                {
                    "type": e.get("type"),
                    "loc": list(loc) if isinstance(loc, (list, tuple)) else loc,
                    "msg": e.get("msg"),
                }
            )
        else:
            cleaned.append({"msg": str(e)})
    return cleaned


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # Spec §2.1: malformed bodies -> HTTP 400 with {accepted:false, reason, details}.
        return JSONResponse(
            status_code=400,
            content={
                "accepted": False,
                "reason": "invalid_payload",
                "details": {"errors": _safe_errors(exc.errors())},
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=detail)
