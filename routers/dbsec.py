"""
DB증권 API Router for KOSPI200 Futures Monitoring
FastAPI router for DB증권 integration endpoints
"""
import os
import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from utils.token_manager import get_token_manager, init_token_manager, shutdown_token_manager
from services.dbsec_ws import get_futures_monitor, start_futures_monitoring, stop_futures_monitoring
from services.dbsec_rest import get_futures_poller, start_futures_polling, stop_futures_polling

logger = logging.getLogger(__name__)

# Create router with prefix
router = APIRouter(prefix="/sentinel/dbsec", tags=["dbsec"])


# Response models
class HealthResponse(BaseModel):
    status: str
    token_manager: Dict[str, Any]
    futures_monitor: Dict[str, Any]
    message: str


class TickData(BaseModel):
    timestamp: str
    symbol: str
    price: float
    volume: int
    session: str
    change_rate: float


class StreamResponse(BaseModel):
    count: int
    buffer_size: int
    recent_ticks: List[TickData]
    last_update: Optional[str]


class AlertEvent(BaseModel):
    symbol: str
    session: str
    change: float
    price: float
    timestamp: str
    threshold: float
    alert_type: str


# Global startup flag to prevent multiple initializations
_initialized = False


@router.on_event("startup")
async def startup_dbsec():
    """Initialize DB증권 services on router startup"""
    global _initialized
    if _initialized:
        return
        
    try:
        logger.info("Initializing DB증권 K200 선물지수 monitoring services...")
        
        # Check if DB증권 is enabled
        dbsec_enabled = os.getenv("DBSEC_ENABLE", "false").lower() in ["true", "1", "yes"]
        if not dbsec_enabled:
            logger.info("DB증권 services disabled by DBSEC_ENABLE=false")
            _initialized = True
            return
        
        # Initialize token manager
        await init_token_manager()
        
        # Use REST API polling instead of WebSocket (more stable)
        use_rest = os.getenv("DBSEC_USE_REST", "true").lower() in ["true", "1", "yes"]
        
        if use_rest:
            # Start REST API polling
            await start_futures_polling()
            logger.info("DB증권 REST API polling started")
        else:
            # Start WebSocket monitoring (legacy)
            try:
                await start_futures_monitoring()
                # Setup alert callback
                futures_monitor = get_futures_monitor()
                futures_monitor.set_alert_callback(alert_callback)
                logger.info("DB증권 WebSocket monitoring started")
            except Exception as ws_error:
                logger.warning(f"WebSocket monitoring failed, falling back to REST: {ws_error}")
                # Fallback to REST if WebSocket fails
                await start_futures_polling()
                logger.info("DB증권 REST API polling started (fallback)")
        
        _initialized = True
        logger.info("DB증권 services initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize DB증권 services: {e}")
        # Don't raise - allow main app to continue working
        logger.warning("DB증권 services will be disabled, main sentinel system continues")


@router.on_event("shutdown")
async def shutdown_dbsec():
    """Shutdown DB증권 services"""
    global _initialized
    if not _initialized:
        return
        
    try:
        logger.info("Shutting down DB증권 services...")
        
        # Stop monitoring
        use_rest = os.getenv("DBSEC_USE_REST", "true").lower() in ["true", "1", "yes"]
        if use_rest:
            await stop_futures_polling()
        else:
            await stop_futures_monitoring()
        
        # Shutdown token manager
        await shutdown_token_manager()
        
        _initialized = False
        logger.info("DB증권 services shutdown completed")
        
    except Exception as e:
        logger.error(f"Error during DB증권 services shutdown: {e}")


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Check DB증권 module health status
    
    Returns:
        - Token manager status and token validity
        - WebSocket connection status
        - Monitoring buffer status
    """
    try:
        # Check token manager
        token_manager = get_token_manager()
        if not token_manager:
            raise HTTPException(
                status_code=503, 
                detail="Token manager not available - check DB_APP_KEY and DB_APP_SECRET"
            )
        
        token_health = await token_manager.health_check()
        
        # Check monitor status
        use_rest = os.getenv("DBSEC_USE_REST", "true").lower() in ["true", "1", "yes"]
        
        if use_rest:
            # REST API poller status
            poller = get_futures_poller()
            monitor_health = {
                "mode": "REST_API",
                "enabled": True,
                "connected": poller.is_running if hasattr(poller, 'is_running') else False,
                "last_price": poller.last_price if hasattr(poller, 'last_price') else None,
                "poll_interval": poller.poll_interval if hasattr(poller, 'poll_interval') else 300
            }
        else:
            # WebSocket monitor status
            futures_monitor = get_futures_monitor()
            monitor_health = futures_monitor.get_health_status()
        
        # Determine overall status
        overall_status = "healthy"
        if not token_health.get("token_valid"):
            overall_status = "degraded"
        if not monitor_health.get("connected"):
            overall_status = "degraded" if overall_status == "healthy" else "unhealthy"
            
        return HealthResponse(
            status=overall_status,
            token_manager=token_health,
            futures_monitor=monitor_health,
            message=f"DB증권 module is {overall_status}"
        )
        
    except Exception as e:
        logger.error(f"Health check error: {e}")
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")


@router.get("/stream", response_model=StreamResponse)
async def get_stream_data(limit: Optional[int] = None):
    """
    Get recent KOSPI200 futures tick data from buffer
    
    Args:
        limit: Maximum number of recent ticks to return (default: all)
        
    Returns:
        Recent tick data with metadata
    """
    try:
        futures_monitor = get_futures_monitor()
        
        # Get recent ticks
        recent_ticks = futures_monitor.get_recent_ticks(limit)
        
        # Convert to response format
        tick_models = []
        for tick in recent_ticks:
            tick_models.append(TickData(
                timestamp=tick["timestamp"],
                symbol=tick["symbol"],
                price=tick["price"],
                volume=tick["volume"],
                session=tick["session"],
                change_rate=tick["change_rate"]
            ))
        
        # Get last update timestamp
        last_update = None
        if tick_models:
            last_update = tick_models[-1].timestamp
            
        return StreamResponse(
            count=len(tick_models),
            buffer_size=len(futures_monitor.tick_buffer),
            recent_ticks=tick_models,
            last_update=last_update
        )
        
    except Exception as e:
        logger.error(f"Stream data error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get stream data: {str(e)}")


@router.post("/restart")
async def restart_monitoring(background_tasks: BackgroundTasks):
    """
    Restart the KOSPI200 futures monitoring
    
    Useful for recovering from connection issues or applying new configurations
    """
    try:
        # Stop current monitoring
        await stop_futures_monitoring()
        
        # Start new monitoring in background
        background_tasks.add_task(start_futures_monitoring)
        
        return {"message": "Monitoring restart initiated", "status": "success"}
        
    except Exception as e:
        logger.error(f"Restart monitoring error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to restart monitoring: {str(e)}")


@router.post("/token/refresh")
async def refresh_token():
    """
    Manually refresh the DB증권 access token
    
    Returns:
        Token refresh status and new token info
    """
    try:
        token_manager = get_token_manager()
        if not token_manager:
            raise HTTPException(
                status_code=503, 
                detail="Token manager not available"
            )
        
        # Force token refresh
        token = await token_manager.get_token()
        if not token:
            raise HTTPException(
                status_code=500,
                detail="Token refresh failed"
            )
        
        # Get updated health status
        health = await token_manager.health_check()
        
        return {
            "message": "Token refreshed successfully",
            "token_valid": health.get("token_valid"),
            "expires_at": health.get("expires_at")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {str(e)}")


@router.get("/config")
async def get_config():
    """
    Get current DB증권 module configuration
    
    Returns:
        Current configuration settings (sensitive values masked)
    """
    import os
    
    return {
        "alert_threshold": float(os.getenv("DB_ALERT_THRESHOLD", "1.0")),
        "buffer_size": int(os.getenv("DB_BUFFER_SIZE", "100")),
        "api_base_url": os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443"),
        "ws_url": os.getenv("DB_WS_URL", "wss://openapi.dbsec.co.kr:9443/ws"),
        "caia_agent_url": os.getenv("CAIA_AGENT_URL", "").strip() or None,
        "app_key_configured": bool(os.getenv("DB_APP_KEY")),
        "app_secret_configured": bool(os.getenv("DB_APP_SECRET"))
    }


@router.post("/alert/test")
async def test_alert():
    """
    Send a test alert to verify the alert system
    
    Useful for testing Caia Agent integration
    """
    try:
        futures_monitor = get_futures_monitor()
        
        # Create test alert payload
        test_payload = {
            "symbol": "K200_FUT",
            "session": "TEST",
            "change": 1.5,  # Simulate 1.5% change
            "price": 350.0,
            "timestamp": "2024-01-01T12:00:00Z",
            "threshold": 1.0,
            "alert_type": "test_alert"
        }
        
        # Send to Caia Agent
        await futures_monitor._send_to_caia_agent(test_payload)
        
        return {
            "message": "Test alert sent successfully",
            "payload": test_payload,
            "status": "success"
        }
        
    except Exception as e:
        logger.error(f"Test alert error: {e}")
        raise HTTPException(status_code=500, detail=f"Test alert failed: {str(e)}")


@router.get("/sessions")
async def get_trading_sessions():
    """
    Get information about KOSPI200 futures trading sessions
    
    Returns:
        Trading session information and current status
    """
    from app.utils import determine_trading_session

    # 세션 정보는 공용 헬퍼로 판정
    session_info = determine_trading_session()
    current_session = session_info.get("session")

    return {
        "current_session": current_session,
        "sessions": {
            "DAY": {
                "name": "주간거래",
                "hours": "09:00 - 15:30 KST",
                "description": "Regular trading session"
            },
            "NIGHT": {
                "name": "야간거래", 
                "hours": "18:00 - 05:00 KST (next day)",
                "description": "After-hours trading session"
            }
        },
        "timezone": "Asia/Seoul"
    }


# Custom alert callback for integration with existing Sentinel
def alert_callback(alert_data: Dict[str, Any]):
    """
    Custom callback for handling alerts from the futures monitor
    
    This can be used to integrate with existing Sentinel alert mechanisms
    """
    logger.info(f"DB증권 Alert callback triggered: {alert_data}")
    
    # Here you could add integration with existing Sentinel alert system
    # For example, sending to Telegram, Discord, etc.
    
    # Example: Format for existing Sentinel alert format
    sentinel_alert = {
        "index": f"DB_{alert_data['symbol']}",
        "value": alert_data["change"],
        "threshold": alert_data["threshold"],
        "session": alert_data["session"],
        "timestamp": alert_data["timestamp"],
        "source": "dbsec"
    }
    
    logger.info(f"Formatted Sentinel alert: {sentinel_alert}")


# Alert callback is set in the main startup function