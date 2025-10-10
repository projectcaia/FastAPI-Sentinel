"""
DB증권 REST API Client for K200 Futures
Polls current price periodically instead of WebSocket
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from collections import deque

import httpx

from utils.token_manager import get_token_manager
from utils.trading_session import determine_trading_session, KST

logger = logging.getLogger(__name__)


class K200FuturesPoller:
    """Polls KODEX 200 ETF price via REST API as K200 proxy"""
    
    def __init__(self):
        self.api_base = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
        self.futures_code = os.getenv("DB_FUTURES_CODE", "101V3000").strip()
        self.poll_interval = int(os.getenv("DB_POLL_INTERVAL_SEC", "180"))  # 3분 기본값
        
        # Price tracking
        self.last_price: Optional[float] = None
        self.daily_open_price: Optional[float] = None
        self.price_buffer: deque = deque(maxlen=100)
        
        # Alert thresholds (same as market_watcher)
        self.thresholds = {
            "LV1": 0.8,
            "LV2": 1.5,
            "LV3": 2.5
        }
        
        self.last_alert_time: Optional[datetime] = None
        self.is_running = False
        
    async def get_current_price(self) -> Optional[Dict[str, Any]]:
        """Fetch current K200 futures price via REST API"""
        try:
            token_manager = get_token_manager()
            if not token_manager:
                logger.error("[DBSEC] Token manager not available")
                return None
                
            token = await token_manager.get_token()
            if not token:
                logger.error("[DBSEC] Failed to get access token")
                return None
            
            # DB증권 정식 API 엔드포인트 사용 - POST 방식
            # KODEX 200 ETF (069500) 사용하여 K200 추적
            etf_code = "069500"  # KODEX 200 ETF (A 제거)
            url = f"{self.api_base}/api/v1/quote/kr-stock/inquiry/price"
            
            # DB증권 정식 API 헤더
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "cont_yn": "",
                "cont_key": ""
            }
            
            # DB증권 정식 API 바디 구조
            request_body = {
                "In": {
                    "InputCondMrktDivCode": "J",  # 주식 시장
                    "InputIscd1": etf_code  # KODEX 200 ETF 코드
                }
            }
            
            async with httpx.AsyncClient(verify=False) as client:  # DB증권 샘플에서 SSL 검증 비활성화
                response = await client.post(url, headers=headers, json=request_body, timeout=10.0)
                
                if response.status_code != 200:
                    logger.error(f"[DBSEC] API request failed: {response.status_code}, URL: {url}")
                    try:
                        error_data = response.json()
                        logger.error(f"[DBSEC] Error response: {error_data}")
                    except:
                        logger.error(f"[DBSEC] Error text: {response.text}")
                    return None
                
                data = response.json()
                logger.info(f"[DBSEC] API Response structure: {list(data.keys())}")
                # 첫 번째 실행에서만 전체 응답을 로그로 출력 (디버깅용)
                if not hasattr(self, '_debug_logged'):
                    logger.info(f"[DBSEC] Full API Response (first time): {data}")
                    self._debug_logged = True
                
                # DB증권 API 응답 구조 확인
                if "Out" not in data:
                    logger.error(f"[DBSEC] Invalid response format: {list(data.keys())}")
                    return None
                    
                output = data.get("Out", {})
                
                # DB증권 API 응답 필드 파싱 (정확한 필드명은 응답 확인 후 조정)
                # 일반적으로 현재가, 시가, 거래량 등의 필드가 있을 것임
                current_price = 0
                open_price = 0
                volume = 0
                
                # 응답 필드 탐색 (로그로 실제 필드명 확인)
                price_fields = ["stck_prpr", "prpr", "price", "current_price", "now_prc", "curr_prc"]
                open_fields = ["stck_oprc", "oprc", "open_price", "open_prc"]
                volume_fields = ["acml_vol", "volume", "day_volume", "tot_vol"]
                
                for field in price_fields:
                    if field in output and output[field]:
                        try:
                            current_price = float(output[field])
                            if current_price > 0:
                                break
                        except (ValueError, TypeError):
                            continue
                            
                for field in open_fields:
                    if field in output and output[field]:
                        try:
                            open_price = float(output[field])
                            if open_price > 0:
                                break
                        except (ValueError, TypeError):
                            continue
                            
                for field in volume_fields:
                    if field in output and output[field]:
                        try:
                            volume = int(output[field])
                            break
                        except (ValueError, TypeError):
                            continue
                
                if current_price <= 0:
                    logger.error(f"[DBSEC] No valid current price found in response fields: {list(output.keys())}")
                    # 응답 전체 로깅으로 디버깅
                    logger.error(f"[DBSEC] Full response for debugging: {output}")
                    return None
                    
                # Store daily open price
                if not self.daily_open_price and open_price > 0:
                    self.daily_open_price = open_price
                elif not self.daily_open_price:
                    self.daily_open_price = current_price  # Fallback
                
                # Calculate change rate
                change_rate = 0.0
                if self.daily_open_price and self.daily_open_price > 0:
                    change_rate = ((current_price - self.daily_open_price) / self.daily_open_price) * 100
                
                # KODEX 200은 실제 ETF 가격 (K200 지수 스케일 조정 불필요)
                # 실제로는 ETF 자체 변동률만 추적
                
                return {
                    "price": current_price,  # ETF 현재가
                    "change_rate": change_rate,
                    "volume": volume,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                
        except Exception as e:
            logger.error(f"[DBSEC] Failed to get price: {e}")
            return None
            
    async def check_and_alert(self, price_data: Dict[str, Any]):
        """Check price change and send alert if needed"""
        change_rate = price_data.get("change_rate", 0)
        abs_change = abs(change_rate)
        
        # Determine level
        level = None
        if abs_change >= self.thresholds["LV3"]:
            level = "LV3"
        elif abs_change >= self.thresholds["LV2"]:
            level = "LV2"
        elif abs_change >= self.thresholds["LV1"]:
            level = "LV1"
            
        if not level:
            return
            
        # Rate limit alerts (1 per minute)
        now = datetime.now(timezone.utc)
        if self.last_alert_time and (now - self.last_alert_time).total_seconds() < 60:
            return
            
        self.last_alert_time = now
        
        # Send alert
        await self.send_alert({
            "symbol": "KODEX200",
            "level": level,
            "change": change_rate,
            "price": price_data["price"],
            "timestamp": price_data["timestamp"]
        })
        
    async def send_alert(self, alert_data: Dict[str, Any]):
        """Send alert to Sentinel"""
        try:
            sentinel_url = os.getenv("SENTINEL_BASE_URL", "").strip()
            if not sentinel_url:
                logger.warning("[DBSEC] SENTINEL_BASE_URL not configured")
                return
                
            # Format for Sentinel
            payload = {
                "index": "KODEX 200",  # KODEX 200 ETF로 표시
                "symbol": "069500.KS",
                "level": alert_data["level"],
                "delta_pct": round(alert_data["change"], 2),
                "triggered_at": alert_data["timestamp"],
                "note": f"{'상승' if alert_data['change'] > 0 else '하락'} {abs(alert_data['change']):.2f}%",
                "kind": "ETF"
            }
            
            headers = {"Content-Type": "application/json"}
            sentinel_key = os.getenv("SENTINEL_KEY", "").strip()
            if sentinel_key:
                headers["x-sentinel-key"] = sentinel_key
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{sentinel_url}/sentinel/alert",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.is_success:
                    logger.info(f"[DBSEC] Alert sent: KODEX 200 {alert_data['change']:.2f}% Level {alert_data['level']}")
                    
        except Exception as e:
            logger.error(f"[DBSEC] Failed to send alert: {e}")
            
    async def start_polling(self):
        """Start periodic price polling"""
        self.is_running = True
        logger.info(f"[DBSEC] Starting KODEX 200 polling (interval: {self.poll_interval}s)")
        
        while self.is_running:
            try:
                # Check trading session - KR 시간대만 확인
                session = determine_trading_session()
                current_session = session.get("session", "UNKNOWN")
                
                if current_session in ["DAY"]:  # 한국 주간 세션만 감시
                    # Get current price
                    price_data = await self.get_current_price()
                    if price_data:
                        self.last_price = price_data["price"]
                        self.price_buffer.append(price_data)
                        
                        logger.info(f"[DBSEC] KODEX 200 ETF: {price_data['price']:.2f}₩ ({price_data['change_rate']:+.2f}%)")
                        
                        # Check for alerts
                        await self.check_and_alert(price_data)
                    else:
                        logger.warning("[DBSEC] Failed to get price data")
                elif current_session == "CLOSED":
                    # Reset daily open price when market is closed
                    if self.daily_open_price is not None:
                        logger.info("[DBSEC] Market closed, resetting daily prices")
                        self.daily_open_price = None
                    logger.debug("[DBSEC] Market closed, waiting...")
                else:
                    # Night session or other - skip for KR ETF
                    logger.debug(f"[DBSEC] Skipping {current_session} session for KR ETF")
                        
            except Exception as e:
                logger.error(f"[DBSEC] Polling error: {e}")
                
            # Wait for next poll
            await asyncio.sleep(self.poll_interval)
            
    async def stop_polling(self):
        """Stop polling"""
        self.is_running = False
        logger.info("[DBSEC] Stopped KODEX 200 polling")


# Global instance
_futures_poller: Optional[K200FuturesPoller] = None


def get_futures_poller() -> K200FuturesPoller:
    """Get global futures poller instance"""
    global _futures_poller
    if _futures_poller is None:
        _futures_poller = K200FuturesPoller()
    return _futures_poller


async def start_futures_polling():
    """Start futures polling in background"""
    poller = get_futures_poller()
    asyncio.create_task(poller.start_polling())
    logger.info("[DBSEC] KODEX 200 ETF REST polling started")


async def stop_futures_polling():
    """Stop futures polling"""
    global _futures_poller
    if _futures_poller:
        await _futures_poller.stop_polling()
        _futures_poller = None