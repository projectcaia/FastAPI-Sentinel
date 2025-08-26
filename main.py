from fastapi import FastAPI
from hub_patch import register_hub_forwarder

app = FastAPI()
register_hub_forwarder(app)

# 기존 라우터/엔드포인트 등록 로직은 그대로 유지
