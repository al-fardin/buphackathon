from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="GridWise AI Energy Optimizer", version="1.0.0",
    docs_url=os.getenv("ENABLE_DOCS", "true").lower() == "true" and "/docs" or None,
    redoc_url=None,
)
app.include_router(router)


@app.exception_handler(json.JSONDecodeError)
async def malformed_json(_: Request, __: json.JSONDecodeError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "Malformed JSON request"})


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"location": ".".join(map(str, item["loc"])), "message": item["msg"]}
              for item in exc.errors()]
    return JSONResponse(status_code=400, content={"detail": "Invalid request", "errors": errors})


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > 1_000_000:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"})
    return await call_next(request)
