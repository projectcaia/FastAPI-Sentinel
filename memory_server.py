# memory_server.py — Proxy 안정화 v1.2
# (재시도/에러핸들/URL 보정/타임아웃 기본 + CORS + 진단 + 수동학습/세션마감 프록시)

import os
from typing import Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Dummy FastAPI 앱 (Railway 외부 확인용/스탠드얼론 프록시)
try:
    from fastapi import FastAPI, Body, Request
    from fastapi.responses import JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    app = FastAPI(title="Caia Memory Proxy")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    app = None  # FastAPI가 없으면 모듈 임포트만 통과


# ───────────── 유틸 ─────────────
def _sanitize_env(s: Optional[str]) -> str:
    if not s:
        return ""
    bad = {ord(c): None for c in "\t\r\n \u200b\u200c\u200d\ufeff"}
    return s.strip().translate(bad)

def _ensure_scheme(url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    return "https://" + url

def _session_with_retries() -> requests.Session:
    s = requests.Session()
    r = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=r))
    s.mount("https://", HTTPAdapter(max_retries=r))
    return s


# ───────────── 베이스 URL ─────────────
def _resolve_base() -> str:
    """
    FUNCTION_CALLING_URL / PUBLIC_BASE_URL 모두 허용:
      - https://host
      - https://host/memory
      - https://host/memory/invoke
    최종 반환: https://host  (스킴 자동 보정)
    """
    fc = _sanitize_env(os.getenv("PUBLIC_BASE_URL") or os.getenv("FUNCTION_CALLING_URL", "http://localhost:8080"))
    fc = fc.rstrip("/")
    if fc.endswith("/memory/invoke"):
        fc = fc[:-len("/memory/invoke")]
    elif fc.endswith("/memory"):
        fc = fc[:-len("/memory")]
    return _ensure_scheme(fc)

def _url(path: str) -> str:
    # path는 반드시 "/"로 시작해야 한다
    if not path.startswith("/"):
        path = "/" + path
    return f"{_resolve_base()}{path}"


# ───────────── 공통 프록시 호출자 ─────────────
_PASSTHRU_HEADERS = {"authorization", "x-caia-session", "x-api-key"}

def _extract_fwd_headers(req: Optional["Request"]) -> Dict[str, str]:
    """보안에 영향 없는 범위에서 헤더 패스스루(토큰류/세션 식별 등)."""
    if req is None:
        return {}
    out: Dict[str, str] = {}
    for k in _PASSTHRU_HEADERS:
        v = req.headers.get(k)
        if v:
            out[k] = v
    return out

def _proxy_post(path: str, payload: Any, timeout: float = 20.0, req: Optional["Request"] = None) -> Dict[str, Any]:
    try:
        with _session_with_retries() as s:
            headers = {"content-type": "application/json", **_extract_fwd_headers(req)}
            r = s.post(_url(path), json=payload, timeout=(5.0, timeout), headers=headers)
        try:
            content = r.json()
        except Exception:
            content = {"text": (r.text or "")[:500]}
        return {
            "status": r.status_code,
            "ok": (200 <= r.status_code < 300),
            "upstream": _resolve_base(),
            "path": path,
            "data": content,
        }
    except Exception as e:
        return {"status": 599, "ok": False, "upstream": _resolve_base(), "path": path, "error": str(e)}

def _proxy_get(path: str, timeout: float = 8.0, req: Optional["Request"] = None) -> Dict[str, Any]:
    try:
        with _session_with_retries() as s:
            headers = _extract_fwd_headers(req)
            r = s.get(_url(path), timeout=(5.0, timeout), headers=headers)
        try:
            content = r.json()
        except Exception:
            content = {"text": (r.text or "")[:500]}
        return {
            "status": r.status_code,
            "ok": (200 <= r.status_code < 300),
            "upstream": _resolve_base(),
            "path": path,
            "data": content,
        }
    except Exception as e:
        return {"status": 599, "ok": False, "upstream": _resolve_base(), "path": path, "error": str(e)}


# ───────────── FastAPI 라우트 ─────────────
if app:
    @app.get("/health")
    def health_check():
        return {"status": "ok", "upstream": _resolve_base()}

    @app.get("/ready")
    async def ready_proxy(req: Request):
        res = _proxy_get("/ready", timeout=8.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 503, content=res)

    # 진단: 환경/베이스 확인
    @app.get("/diag/env")
    def diag_env():
        return {
            "PUBLIC_BASE_URL": os.getenv("PUBLIC_BASE_URL"),
            "FUNCTION_CALLING_URL": os.getenv("FUNCTION_CALLING_URL"),
            "resolved_base": _resolve_base(),
        }

    @app.get("/diag/base")
    def diag_base():
        return {"resolved_base": _resolve_base()}

    # Echo 프록시: 메시지 배열/래핑 모두 지원
    @app.post("/memory/echo")
    async def echo_proxy(body: Any = Body(None), req: Request = None):
        payload = body if body is not None else {"messages": [{"type": "human", "content": "ping"}]}
        res = _proxy_post("/memory/echo", payload, timeout=20.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)

    # Retrieve 프록시
    @app.post("/memory/retrieve")
    async def retrieve_proxy(body: Dict[str, Any] = Body(None), req: Request = None):
        payload = body.copy() if body is not None else {"query": "test"}
        # only_rules 파라미터가 true이면 recall_rules 모드를 사용
        if payload.get("only_rules"):
            payload.setdefault("mode", "recall_rules")
        res = _proxy_post("/memory/retrieve", payload, timeout=20.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)

    # Invoke 프록시: type/topic/tags 전달 보존
    @app.post("/memory/invoke")
    async def invoke_proxy(body: Dict[str, Any] = Body(None), req: Request = None):
        payload = body if body is not None else {
            "messages": [{"type": "system", "topic": "proxy", "content": "[proxy invoke]"}]
        }
        res = _proxy_post("/memory/invoke", payload, timeout=30.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)

    # 🔹 수동 학습 프록시(대화 중 즉시 학습 경로)
    @app.post("/memory/manual_learn")
    async def manual_learn_proxy(body: Dict[str, Any] = Body(None), req: Request = None):
        payload = body.copy() if body is not None else {"text": "empty", "topic": "피드백", "tags": ["proxy"]}
        # save_ersp 모드로 자동 전환: 사건→성찰→규칙→트리거 생성 허용
        payload.setdefault("mode", "save_ersp")
        payload.setdefault("allow_transform", True)
        # text 필드를 event로 복사하여 ERSP 저장용으로 사용
        if payload.get("text") and not payload.get("event"):
            payload["event"] = payload["text"]
        res = _proxy_post("/memory/manual_learn", payload, timeout=20.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)

    # 🔹 세션 마감 요약 프록시(L2 생성)
    @app.post("/memory/finalize_session")
    async def finalize_session_proxy(body: Dict[str, Any] = Body(None), req: Request = None):
        payload = body if body is not None else {"hours": 24}
        res = _proxy_post("/memory/finalize_session", payload, timeout=30.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)

    # (선택) 서버 진단 라우트도 패스스루
    @app.post("/diag/qdrant")
    async def diag_qdrant_proxy(req: Request):
        res = _proxy_post("/diag/qdrant", payload={}, timeout=20.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)

    @app.post("/diag/roundtrip")
    async def diag_roundtrip_proxy(req: Request):
        res = _proxy_post("/diag/roundtrip", payload={}, timeout=20.0, req=req)
        return JSONResponse(status_code=res["status"] if res["ok"] else 502, content=res)


if __name__ == "__main__" and app:
    import uvicorn
    port = int(os.environ.get("PORT", 8090))
    print(f"[Caia Proxy] Upstream = {_resolve_base()}  (PORT={port})")
    uvicorn.run("memory_server:app", host="0.0.0.0", port=port)
