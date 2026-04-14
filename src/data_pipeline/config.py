"""Shared configuration loader for data pipeline modules.

All .env access happens here. Other modules import constants, never call load_dotenv.
"""
import os
from dotenv import load_dotenv

load_dotenv()

KRX_API_KEY = os.getenv("KRX_API_KEY")
DART_API_KEY = os.getenv("DART_API_KEY")

KRX_DAILY_CALL_LIMIT = 10_000
KRX_CALL_WARN_THRESHOLD = 9_500

if not KRX_API_KEY:
    raise RuntimeError(
        "KRX_API_KEY not set in .env. "
        "Configure it before running data pipeline modules."
    )
