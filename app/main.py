from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from app.body_limit import BodySizeLimitMiddleware
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.kb import router as kb_router
from app.logs import configure_logging
from app.operator import router as operator_router
from app.webhooks import router as webhooks_router


def create_app(settings: Settings) -> FastAPI:
    engine = create_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    application = FastAPI(title="Support drafts", lifespan=lifespan)
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.abandoned_writes = set()
    application.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    application.include_router(webhooks_router)
    application.include_router(kb_router)
    application.include_router(operator_router)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes the input, which leaks PII and crashes on lone surrogates.
        errors = [{"loc": error["loc"], "msg": error["msg"]} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @application.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "OK"

    return application


configure_logging(get_settings().log_level)
app = create_app(get_settings())
