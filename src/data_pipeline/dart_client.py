"""Minimal DART OpenAPI client.

Phase 1 Step 1b-3: corp_code download only. Other DART endpoints are out of
scope for this minimal client.
"""
from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import requests
from loguru import logger

from src.data_pipeline.config import DART_API_KEY

BASE_URL = "https://opendart.fss.or.kr/api"


class DartClient:
    """Thin DART OpenAPI wrapper. Currently exposes corp_code download only."""

    def __init__(self) -> None:
        if not DART_API_KEY:
            raise RuntimeError("DART_API_KEY not set in .env")
        self._api_key = DART_API_KEY.strip()
        self._session = requests.Session()

    def download_corp_code(self, save_path: Path) -> Path:
        """Download corp_code ZIP and extract CORPCODE.xml to save_path.

        Returns the saved XML path.
        """
        url = f"{BASE_URL}/corpCode.xml"
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        resp = self._session.get(
            url, params={"crtfc_key": self._api_key}, timeout=60
        )
        elapsed_dl = time.monotonic() - started
        resp.raise_for_status()

        size_mb = len(resp.content) / (1024 * 1024)
        logger.info(
            f"DART corp_code downloaded: {size_mb:.2f} MB in {elapsed_dl:.2f}s"
        )

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            xml_name = next(
                (n for n in names if n.upper().endswith(".XML")), None
            )
            if xml_name is None:
                raise RuntimeError(
                    f"No XML file in DART corp_code zip. Members: {names}"
                )
            with zf.open(xml_name) as src, open(save_path, "wb") as dst:
                dst.write(src.read())

        xml_size_mb = save_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"Extracted {xml_name} → {save_path} ({xml_size_mb:.2f} MB)"
        )
        return save_path
