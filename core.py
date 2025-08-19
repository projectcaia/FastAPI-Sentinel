from app_routes_sentinel import router as fc_sentinel_router

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8787, reload=True)

