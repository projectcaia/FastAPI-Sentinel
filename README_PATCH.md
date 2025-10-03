# Sentinel Patch for ASCII idempotency key (2025‑08‑30)

This folder contains a patched version of the Sentinel `main.py` file that
addresses the Unicode encoding error encountered when forwarding alerts to
ConnectorHub.

## Problem

When Sentinel forwards alerts to ConnectorHub it uses the alert’s `index` and
timestamp to build an `idempotency_key`. Some indices include the Greek
character `Δ` (e.g., `ΔVIX`, `ΔSPX`). HTTP headers only support ASCII, so
including `Δ` in the `Idempotency‑Key` header causes a `UnicodeEncodeError`:

```
Hub forward error: 'ascii' codec can't encode character '\u0394'
```

## Solution

The patched `main.py` sanitizes the index value before constructing the
idempotency key. Non‑ASCII characters are stripped, leaving only letters,
numbers, underscores and hyphens. The sanitized key is used consistently in
both the request body and the HTTP header.

### Key points

* Added `_sanitize_index` helper to remove non‑ASCII characters.
* Sanitization applied when generating a new idempotency key and when a key is
  provided externally.
* Updated logging to clarify patched behaviour.

## How to apply

1. Clone or download the existing `FastAPI‑Sentinel` repository.
2. Replace the original `main.py` file at the repository root with the
   patched `main.py` from this folder.
3. Commit and push your changes to GitHub or your deployment platform (e.g.,
   Railway). Redeploy the service.
4. Watch the logs; alerts containing Greek letters should now forward
   successfully to ConnectorHub.

If you need to restore other files, ensure you merge this patch into your
current codebase rather than deleting existing FastAPI routes or worker code.

## DB증권 API Module (Added 2025-10-03)

### Overview

This repository now includes a new module for **DB증권 API 기반 KOSPI200 선물 주·야간 실시간 감시**. The module provides real-time monitoring of KOSPI200 futures with automatic anomaly detection and alert capabilities.

### Module Structure

- `routers/dbsec.py` - FastAPI router with DB증권 endpoints (`/sentinel/dbsec/`)
- `utils/token_manager.py` - OAuth2 token management with auto-refresh
- `services/dbsec_ws.py` - WebSocket client for real-time market data

### Features

1. **Real-time KOSPI200 Futures Monitoring**
   - 주간거래 (09:00-15:15 KST) and 야간거래 (18:00-05:00 KST) support
   - WebSocket-based real-time tick data streaming
   - Automatic session detection (DAY/NIGHT/UNKNOWN)

2. **Anomaly Detection**
   - Configurable alert threshold (default: ±1% price change)
   - Spam protection (max 1 alert per minute for same condition)
   - Integration with Caia Agent `/report` endpoint

3. **Token Management**
   - Automatic OAuth2 token refresh every 23 hours
   - Thread-safe token operations with asyncio locks
   - Health monitoring and status reporting

4. **WebSocket Resilience**
   - Auto-reconnection with exponential backoff
   - Connection health monitoring
   - Configurable retry limits and timeouts

### Configuration

Set the following environment variables:

```bash
# Required - DB증권 API Credentials
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret

# Optional - API Configuration
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_WS_URL=wss://openapi.dbsec.co.kr:9443/ws
DB_ALERT_THRESHOLD=1.0    # Alert threshold percentage (default: 1.0%)
DB_BUFFER_SIZE=100        # Tick data buffer size (default: 100)

# Optional - Caia Agent Integration
CAIA_AGENT_URL=https://your-caia-agent.com
```

### API Endpoints

After deployment, the following endpoints are available:

- `GET /sentinel/dbsec/health` - Module health check and connectivity status
- `GET /sentinel/dbsec/stream` - Get recent KOSPI200 futures tick data
- `GET /sentinel/dbsec/config` - View current module configuration
- `POST /sentinel/dbsec/restart` - Restart WebSocket monitoring
- `POST /sentinel/dbsec/token/refresh` - Manually refresh access token
- `POST /sentinel/dbsec/alert/test` - Send test alert to verify integration
- `GET /sentinel/dbsec/sessions` - Get trading session information

### Usage Examples

1. **Check module health:**
```bash
curl https://your-sentinel-domain.up.railway.app/sentinel/dbsec/health
```

2. **Get recent market data:**
```bash
curl https://your-sentinel-domain.up.railway.app/sentinel/dbsec/stream?limit=10
```

3. **Test alert system:**
```bash
curl -X POST https://your-sentinel-domain.up.railway.app/sentinel/dbsec/alert/test
```

### Alert Integration

When a price anomaly is detected (change ≥ threshold), the module:

1. **Logs the event** with detailed information
2. **Sends payload to Caia Agent** (if configured):
   ```json
   {
     "symbol": "K200_FUT",
     "session": "DAY|NIGHT",
     "change": 1.5,
     "price": 350.0,
     "timestamp": "2024-01-01T12:00:00Z",
     "threshold": 1.0,
     "alert_type": "price_anomaly"
   }
   ```
3. **Triggers custom callback** for integration with existing Sentinel alerts

### Monitoring & Troubleshooting

- **Health Check**: Use `/sentinel/dbsec/health` to monitor token validity and WebSocket connection
- **Logs**: Check application logs for connection status and alert events
- **Restart**: Use `/sentinel/dbsec/restart` to recover from connection issues
- **Token Issues**: Use `/sentinel/dbsec/token/refresh` to manually refresh tokens

### Deployment Notes

- The module starts automatically when the FastAPI application launches
- WebSocket connections are resilient and will auto-reconnect on failures
- Token refresh runs in background with 23-hour intervals
- All operations are thread-safe and designed for production use

### Development & Testing

For local development:

1. Copy `.env.example` to `.env`
2. Set required DB증권 credentials
3. Run: `uvicorn main:app --reload`
4. Access Swagger docs at: `http://localhost:8000/docs`
