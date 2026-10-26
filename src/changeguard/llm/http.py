from __future__ import annotations

import time

import requests

from ..errors import LLMError

RETRY_STATUS = {429, 500, 502, 503, 504}


def post_json(url: str, headers: dict, body: dict, timeout: float = 60.0, retries: int = 3) -> dict:
    """POST with exponential backoff on rate limits/transient server errors. Never logs headers (they hold keys)."""
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            last = f"network error: {type(e).__name__}"
            if attempt < retries:
                time.sleep(min(8, 2**attempt))
                continue
            raise LLMError(last) from e
        if resp.status_code in RETRY_STATUS and attempt < retries:
            time.sleep(min(8, 2**attempt))
            continue
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise LLMError("provider returned non-JSON") from e
    raise LLMError(last or "provider unavailable")
