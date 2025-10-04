#!/usr/bin/env python3
"""
Test DB증권 Token Manager
Usage: python test_token.py
"""
import asyncio
import os
import logging
import sys

import pytest

pytestmark = pytest.mark.skip(reason="Manual DB증권 token test is excluded from automated pytest runs.")

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.masking import mask_secret, redact_dict
from utils.token_manager import DBSecTokenManager


async def test_token_request():
    """Test token request with current environment variables"""
    
    # Get environment variables
    app_key = os.getenv("DB_APP_KEY", "").strip()
    app_secret = os.getenv("DB_APP_SECRET", "").strip()
    base_url = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
    
    print("\n" + "="*60)
    print("DB증권 Token Manager Test")
    print("="*60)
    
    if not app_key or not app_secret:
        print("⚠️  WARNING: DB_APP_KEY or DB_APP_SECRET not set")
        print("   Set environment variables first:")
        print("   export DB_APP_KEY=your_key")
        print("   export DB_APP_SECRET=your_secret")
        print("="*60)
        return
    
    print(f"📍 Base URL: {base_url}")
    print(f"🔑 App Key: {mask_secret(app_key)}")
    print(f"🔒 Secret: {mask_secret(app_secret)} (length: {len(app_secret)})")
    print("="*60)
    
    # Create token manager
    manager = DBSecTokenManager(
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        enabled=True
    )
    
    print("\n🔄 Requesting token...")
    print("-"*40)
    
    # Try to get token
    token = await manager.get_token()
    
    print("-"*40)
    
    if token:
        print("\n✅ SUCCESS! Token acquired")
        print(f"📝 Token: {mask_secret(token)}")
        print(f"🕐 Expires at: {manager.expires_at}")
        print(f"📋 Type: {manager.token_type}")
    else:
        print("\n❌ FAILED to acquire token")
        print("   Check the logs above for error details")
    
    print("\n" + "="*60)
    
    # Health check
    health = await manager.health_check()
    redacted_health = redact_dict(health)
    print("\n📊 Health Check:")
    for key, value in redacted_health.items():
        print(f"   {key}: {value}")
    
    print("="*60)


if __name__ == "__main__":
    asyncio.run(test_token_request())
