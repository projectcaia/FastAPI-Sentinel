import asyncio, httpx, random
from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
API_URL = "https://api.telegram.org"
async def send_telegram_message(text: str, simulate_429: bool = False, max_retries: int = 3) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN/CHAT_ID missing")
    url = f"{API_URL}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    attempts = 0
    simulate_failures = random.randint(1, 2) if simulate_429 else 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        while attempts < max_retries:
            attempts += 1
            if simulate_429 and attempts <= simulate_failures:
                await asyncio.sleep(0.1); status = 429; err = {"description":"Too Many Requests (simulated)"}
            else:
                try:
                    r = await client.post(url, data=params); status = r.status_code
                    if status == 200: return {"ok": True, "attempts": attempts, "status": status, "data": r.json()}
                    else: err = r.json() if r.headers.get("content-type","").startswith("application/json") else {"description": r.text}
                except Exception as e:
                    status = 0; err = {"description": str(e)}
            if status in (0,429,500,502,503,504) and attempts < max_retries:
                await asyncio.sleep(0.5 * (2 ** (attempts-1))); continue
            return {"ok": False, "attempts": attempts, "status": status, "error": err}
