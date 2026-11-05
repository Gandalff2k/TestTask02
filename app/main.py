from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.logs import configure_logging

configure_logging(get_settings().log_level)

app = FastAPI(title="Support drafts")


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"
