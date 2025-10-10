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
    """Polls K200 futures price via REST API"""
    
    def __init__(self):
        self.api_base = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
        self.futures_code = os.getenv("DB_FUTURES_CODE", "101V3000").strip()
        self.poll_interval = int(os.getenv("DB_POLL_INTERVAL_SEC", "300"))  # 5분 기본값
        
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
            
            # DB증권 선물 현재가 조회 API
            url = f"{self.api_base}/uapi/domestic-futureoption/v1/quotations/inquire-price"
            
            headers = {
                "authorization": f"Bearer {token}",
                "appkey": getattr(token_manager, "app_key", ""),
                "appsecret": getattr(token_manager, "app_secret", ""),
                "tr_id": "FHMIF10000000"  # 선물 현재가 조회
            }
            
            params = {
                "FID_COND_MRKT_DIV_CODE": "F",  # 선물
                "FID_INPUT_ISCD": self.futures_code  # K200 선물 종목코드
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params, timeout=10.0)
                
                if response.status_code != 200:
                    logger.error(f"[DBSEC] API request failed: {response.status_code}")
                    return None
                
                data = response.json()
                
                if data.get("rt_cd") != "0":
                    logger.error(f"[DBSEC] API error: {data.get('msg1', 'Unknown error')}")
                    return None
                    
                output = data.get("output", {})
                
                # Parse price data
                current_price = float(output.get("fut_prpr", 0))  # 선물 현재가
                if current_price <= 0:
                    return None
                    
                # Store open price
                if not self.daily_open_price:
                    self.daily_open_price = float(output.get("fut_oprc", current_price))  # 선물 시가
                
                # Calculate change rate
                change_rate = 0.0
                if self.daily_open_price and self.daily_open_price > 0:
                    change_rate = ((current_price - self.daily_open_price) / self.daily_open_price) * 100
                
                return {
                    "price": current_price,
                    "change_rate": change_rate,
                    "volume": int(output.get("acml_vol", 0)),  # 누적 거래량
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
            "symbol": "K200_FUT",
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
                "index": "K200 선물",
                "symbol": "K200_FUT",
                "level": alert_data["level"],
                "delta_pct": round(alert_data["change"], 2),
                "triggered_at": alert_data["timestamp"],
                "note": f"{'상승' if alert_data['change'] > 0 else '하락'} {abs(alert_data['change']):.2f}%",
                "kind": "FUTURES"
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
                    logger.info(f"[DBSEC] Alert sent: K200 선물 {alert_data['change']:.2f}% Level {alert_data['level']}")
                    
        except Exception as e:
            logger.error(f"[DBSEC] Failed to send alert: {e}")
            
    async def start_polling(self):
        """Start periodic price polling"""
        self.is_running = True
        logger.info(f"[DBSEC] Starting K200 futures polling (interval: {self.poll_interval}s)")
        
        while self.is_running:
            try:
                # Check trading session
                session = determine_trading_session()
                if session.get("session") in ["DAY", "NIGHT"]:
                    # Get current price
                    price_data = await self.get_current_price()
                    if price_data:
                        self.last_price = price_data["price"]
                        self.price_buffer.append(price_data)
                        
                        logger.debug(f"[DBSEC] K200 선물: {price_data['price']:.2f} ({price_data['change_rate']:+.2f}%)")
                        
                        # Check for alerts
                        await self.check_and_alert(price_data)
                else:
                    # Reset daily open price when session changes
                    if session.get("session") == "CLOSED":
                        self.daily_open_price = None
                        logger.debug("[DBSEC] Market closed, waiting...")
                        
            except Exception as e:
                logger.error(f"[DBSEC] Polling error: {e}")
                
            # Wait for next poll
            await asyncio.sleep(self.poll_interval)
            
    async def stop_polling(self):
        """Stop polling"""
        self.is_running = False
        logger.info("[DBSEC] Stopped K200 futures polling")


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
    logger.info("[DBSEC] K200 futures REST polling started")


async def stop_futures_polling():
    """Stop futures polling"""
    global _futures_poller
    if _futures_poller:
        await _futures_poller.stop_polling()
        _futures_poller = None