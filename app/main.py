from fastapi import FastAPI
from app.sentinel_router import router as sentinel_router

app = FastAPI()
app.include_router(sentinel_router)
