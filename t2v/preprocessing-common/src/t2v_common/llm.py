import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from t2v_common.config import LLMEndpointConfig
from t2v_common.error_tracker import NetworkError, ValidationError

logger = logging.getLogger(__name__)


class RateLimiter:
    """Thread-safe rate limiter using a sliding window of request timestamps."""

    def __init__(self, max_requests_per_minute: int):
        self.max_rpm = max_requests_per_minute
        self.enabled = max_requests_per_minute > 0
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request is allowed under the rate limit."""
        if not self.enabled:
            return

        while True:
            with self._lock:
                now = time.monotonic()
                # Evict timestamps older than 60 seconds
                while self._timestamps and self._timestamps[0] <= now - 60.0:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_rpm:
                    self._timestamps.append(now)
                    return

                # Calculate how long to wait until the oldest timestamp expires
                wait = 60.0 - (now - self._timestamps[0]) + 0.05

            logger.debug("Rate limiter: sleeping %.2fs", wait)
            time.sleep(wait)


class LLMClient:
    """LLM client supporting Ollama and OpenAI-compatible APIs.

    Generation calls use structured JSON output (JSON schema).
    Embedding calls return float vectors.
    Retry logic is NOT handled here — callers use ErrorTracker.
    """

    def __init__(self, config: LLMEndpointConfig):
        self.config = config
        self.endpoint = config.endpoint.rstrip("/")
        self.api_type = config.api_type
        self.model = config.model
        self.session = requests.Session()
        self._rate_limiter = RateLimiter(config.max_requests_per_minute)
        if config.api_key:
            self.session.headers["Authorization"] = f"Bearer {config.api_key}"

    def generate(self, prompt: str, json_schema: dict) -> dict:
        """Generate structured JSON output from a prompt.

        Returns the parsed JSON dict. Raises NetworkError on connection/server
        errors, ValidationError on unparseable responses.
        """
        if self.api_type == "ollama":
            return self._generate_ollama(prompt, json_schema)
        else:
            return self._generate_openai(prompt, json_schema)

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text.

        Raises NetworkError on connection/server errors.
        """
        if self.api_type == "ollama":
            return self._embed_ollama(text)
        else:
            return self._embed_openai(text)

    def generate_batch(self, prompts: list[str], json_schema: dict) -> list[dict]:
        """Generate structured JSON for multiple prompts.

        Ollama: sequential calls. OpenAI: parallel via ThreadPoolExecutor.
        """
        if self.api_type == "ollama":
            return [self.generate(p, json_schema) for p in prompts]
        else:
            return self._parallel_map(lambda p: self.generate(p, json_schema), prompts)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Ollama: sequential calls. OpenAI: parallel via ThreadPoolExecutor.
        """
        if self.api_type == "ollama":
            return [self.embed(t) for t in texts]
        else:
            return self._parallel_map(lambda t: self.embed(t), texts)

    # --- Ollama implementation ---

    def _generate_ollama(self, prompt: str, json_schema: dict) -> dict:
        data = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": json_schema,
        }
        resp = self._post("/api/chat", data)
        content = resp["message"]["content"]
        return self._parse_json(content)

    def _embed_ollama(self, text: str) -> list[float]:
        data = {"model": self.model, "input": text}
        resp = self._post("/api/embed", data)
        return resp["embeddings"][0]

    # --- OpenAI implementation ---

    @staticmethod
    def _strict_schema(schema: dict) -> dict:
        """Deep-copy a JSON schema, adding 'additionalProperties': false to all object types."""
        schema = dict(schema)
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
            if "properties" in schema:
                schema["properties"] = {
                    k: LLMClient._strict_schema(v)
                    for k, v in schema["properties"].items()
                }
        if schema.get("type") == "array" and "items" in schema:
            schema["items"] = LLMClient._strict_schema(schema["items"])
        return schema

    def _generate_openai(self, prompt: str, json_schema: dict) -> dict:
        data = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": self._strict_schema(json_schema),
                },
            },
        }
        resp = self._post("/v1/chat/completions", data)
        content = resp["choices"][0]["message"]["content"]
        return self._parse_json(content)

    def _embed_openai(self, text: str) -> list[float]:
        data = {"model": self.model, "input": text}
        resp = self._post("/v1/embeddings", data)
        return resp["data"][0]["embedding"]

    # --- Shared helpers ---

    def _post(self, path: str, data: dict) -> dict:
        """POST JSON to the API endpoint. Maps errors to NetworkError."""
        self._rate_limiter.acquire()

        url = f"{self.endpoint}{path}"
        try:
            resp = self.session.post(url, json=data, timeout=120)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise NetworkError(f"Request to {url} failed: {e}") from e

        if resp.status_code == 429:
            raise NetworkError(f"Rate limited (429) from {url}: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise NetworkError(
                f"Server error {resp.status_code} from {url}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Client error {resp.status_code} from {url}: {resp.text[:500]}"
            )

        try:
            return resp.json()
        except ValueError as e:
            raise NetworkError(
                f"Non-JSON response from {url}: {resp.text[:200]}"
            ) from e

    def _parse_json(self, content: str) -> dict[str, Any]:
        """Parse JSON content from LLM response. Raises ValidationError."""
        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValidationError(
                f"Invalid JSON in LLM response: {e}\nContent: {content[:200]}"
            ) from e
        if not isinstance(result, dict):
            raise ValidationError(
                f"Expected JSON object, got {type(result).__name__}: {content[:200]}"
            )
        return result

    def _parallel_map(self, fn: Any, items: list) -> list:
        """Execute fn on each item in parallel, preserving order."""
        results: list[Any] = [None] * len(items)
        with ThreadPoolExecutor() as executor:
            future_to_idx = {
                executor.submit(fn, item): idx for idx, item in enumerate(items)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()  # re-raises any exception
        return results
