# Trading Session Logic Fix

## Summary
Fixed DB증권 API WebSocket monitoring logic that was incorrectly identifying trading hours as closed periods and sleeping unnecessarily during active trading sessions.

## Problem
The monitoring routine was:
1. Making excessive `compute_next_open_kst()` calls even during active sessions
2. Entering infinite sleep during CLOSED periods instead of periodic rechecks
3. Incorrectly determining session status due to complex logic

## Solution

### 1. New Simplified Module: `utils/trading_session.py`
- Single `is_krx_trading_day()` check per call
- Clear time-based session determination:
  - **DAY**: 09:00 - 15:30 on trading days
  - **NIGHT**: 18:00 - 05:00 on trading days  
  - **CLOSED**: All other times or holidays

### 2. Updated `services/dbsec_ws.py`
- Removed unnecessary `sleep_until()` function
- Simplified `start_monitoring()` loop:
  - Holidays: Skip WebSocket, recheck in 30 minutes
  - CLOSED on trading days: Recheck in 30 minutes  
  - DAY/NIGHT: Connect WebSocket immediately
- Alert routines continue regardless of session state
- WebSocket only disabled on actual holidays (weekends/KRX holidays)

### 3. Backward Compatibility
- `app/utils.py` maintains original interface
- `compute_next_open_kst()` still available but not called unnecessarily
- All existing tests pass without modification

## Testing

### Unit Tests
```bash
pytest tests/test_session.py -xvs
# Result: 13 passed
```

### Manual Verification
Key time points tested:
- ✅ 09:00 - DAY session starts correctly
- ✅ 15:29 - Still in DAY session
- ✅ 15:31 - CLOSED (between sessions)
- ✅ 18:05 - NIGHT session active
- ✅ 04:59 - Still in NIGHT session
- ✅ Weekend - CLOSED with is_holiday=True

### Performance Impact
- Reduced unnecessary date calculations
- No more infinite sleep periods
- Consistent 30-minute recheck intervals during closed periods
- Immediate WebSocket connection during active sessions

## Logging
- **[DBSEC]** prefix maintained
- INFO level: Session changes and WebSocket state only
- DEBUG level: Suppressed for normal operations
- Clear Korean messages for session states

## Environment Variables
No changes to existing configuration:
- `DBSEC_ENABLE`: Enable/disable module
- `DBSEC_POLL_MINUTES`: Recheck interval (default: 30)
- `DBSEC_SLEEP_CAP_HOURS`: Not used in simplified logic
- All other DB증권 settings unchanged

## Deployment Notes
1. No configuration changes required
2. Module will automatically use new logic
3. Monitoring will resume correctly at next trading session
4. Alert integration with MarketWatcher unchanged