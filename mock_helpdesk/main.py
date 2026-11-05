from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Mock helpdesk")


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"
