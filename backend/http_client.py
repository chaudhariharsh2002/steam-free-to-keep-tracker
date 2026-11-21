"""Resilient HTTP client with timeouts, exponential backoff, HTTP 429 Retry-After handling,
malformed JSON protection, and real request/retry/failure telemetry counters.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import httpx


class ResilientHTTPError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, url: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


@dataclass
class RequestTelemetry:
    completed_requests: int = 0
    failed_requests: int = 0
    retried_requests: int = 0
    response_times_ms: Dict[str, list] = field(default_factory=dict)

    def record_latency(self, source: str, ms: float) -> None:
        self.response_times_ms.setdefault(source, []).append(round(ms, 1))

    def average_latencies(self) -> Dict[str, float]:
        return {
            src: round(sum(vals) / len(vals), 1)
            for src, vals in self.response_times_ms.items()
            if vals
        }


class ResilientHTTPClient:
    """Wrapper over httpx.Client with strict retry limits, 429 handling, and telemetry."""

    def __init__(
        self,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.5,
        user_agent: str = "SteamFreeToKeepTracker/1.0",
        sleep_fn: Callable[[float], None] = time.sleep,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.sleep_fn = sleep_fn
        self.telemetry = RequestTelemetry()
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept-Language": "en-IN,en;q=0.9"},
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        url: str,
        source: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        content: Optional[bytes | str] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            start = time.monotonic()
            try:
                resp = self._client.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    content=content,
                    json=json_body,
                )
                elapsed_ms = (time.monotonic() - start) * 1000.0
                self.telemetry.record_latency(source, elapsed_ms)

                if resp.status_code == 429:
                    retry_after_raw = resp.headers.get("Retry-After")
                    delay = self.backoff_base_seconds * (2 ** attempt)
                    if retry_after_raw:
                        try:
                            delay = max(delay, min(float(retry_after_raw), 60.0))
                        except ValueError:
                            pass
                    if attempt < self.max_retries:
                        self.telemetry.retried_requests += 1
                        self.sleep_fn(delay)
                        continue
                    self.telemetry.failed_requests += 1
                    raise ResilientHTTPError(
                        f"HTTP 429 rate limited after {attempt + 1} attempts (Retry-After={retry_after_raw})",
                        status_code=429,
                        url=url,
                    )

                if 500 <= resp.status_code < 600:
                    if attempt < self.max_retries:
                        self.telemetry.retried_requests += 1
                        self.sleep_fn(self.backoff_base_seconds * (2 ** attempt))
                        continue
                    self.telemetry.failed_requests += 1
                    raise ResilientHTTPError(
                        f"Server error HTTP {resp.status_code} from {source}",
                        status_code=resp.status_code,
                        url=url,
                    )

                if resp.status_code >= 400:
                    self.telemetry.failed_requests += 1
                    raise ResilientHTTPError(
                        f"Client error HTTP {resp.status_code} from {source}",
                        status_code=resp.status_code,
                        url=url,
                    )

                self.telemetry.completed_requests += 1
                return resp

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.telemetry.retried_requests += 1
                    self.sleep_fn(self.backoff_base_seconds * (2 ** attempt))
                    continue
                self.telemetry.failed_requests += 1
                raise ResilientHTTPError(
                    f"Network/timeout error on {source}: {exc}",
                    status_code=None,
                    url=url,
                ) from exc

        self.telemetry.failed_requests += 1
        raise ResilientHTTPError(f"Request failed on {source}: {last_error}", url=url)

    def get_json(
        self,
        url: str,
        source: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> tuple[Any, Dict[str, str]]:
        resp = self.request("GET", url, source=source, params=params, headers=headers)
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            self.telemetry.failed_requests += 1
            raise ResilientHTTPError(
                f"Malformed JSON response from {source}: {exc}",
                status_code=resp.status_code,
                url=url,
            ) from exc
        return data, dict(resp.headers)

    def get_text(
        self,
        url: str,
        source: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> tuple[str, Dict[str, str]]:
        resp = self.request("GET", url, source=source, params=params, headers=headers)
        return resp.text, dict(resp.headers)
