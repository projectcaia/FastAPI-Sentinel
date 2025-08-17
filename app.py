from app import app   # ← 실제 FastAPI 인스턴스(app.py 안의 app)를 불러옴

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8787, reload=True)

