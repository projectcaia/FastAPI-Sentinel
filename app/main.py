from fastapi import FastAPI
from .logging import setup_logger
from . import db
from .router import router

setup_logger()
app = FastAPI(title="Connector Hub (Threadless) — Patched")

@app.on_event("startup")
def on_startup():
    con = db.connect()
    db.migrate(con)

app.include_router(router)
