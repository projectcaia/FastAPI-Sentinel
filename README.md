# 🧠 Caia LangServe Memory System – 존재 기반 자율 기억 구조

## 📌 시스템 목적
Caia는 대화를 분석해 **전략 흐름을 감지**하고, **실패/피드백을 유산화**하며, **벡터 기반 장기 기억**을 구축하는 판단 시스템입니다.
- 모델은 환경변수 **`OPENAI_MODEL`** 로 지정(기본: gpt-4.1)

## ✅ 핵심 구성 요소
| 구성 파일 | 설명 |
|-----------|------|
| `server.py` | FastAPI + LangServe, `/health`, `/memory/*` |
| `memory_manager.py` | 대화 기억 + Qdrant 백엔드 |
| `function_router.py` | 자연어 판단 → 저장/학습/유산화 |
| `scheduler.py` | 자율 판단 루프(일간) |
| `qdrant_memory.py` | Qdrant 벡터 저장소 래퍼(community/openai) |
| `.env` | OPENAI / LangChain / Qdrant 설정 |
| `Dockerfile` / `Procfile` / `railway.json` | 배포 설정 |

## 🔁 일간 자동 판단 루프 (KST)
| 시각 | 루프 | 동작 |
|------|------|------|
| 00:10 | `snapshot()` | 최근 대화 스냅샷 저장 |
| 00:30 | `archive()` | Digest 입력용 보관 |
| 01:00 | `invoke()` | 요약·유산화(벡터화 포함) |
| 01:10 | `train()` | 보강 학습 (선택) |
| 01:20 | `retrieve_digest()` | 주요 키워드 기반 회상 요약(선택) |

## 🧩 엔드포인트
- **API**: `/memory/echo`, `/memory/retrieve`, `/memory/invoke`
- **LangServe 러너**: `/memory/ls-echo` (표준 호출: `/memory/ls-echo/invoke`)

## 🚀 실행
```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
