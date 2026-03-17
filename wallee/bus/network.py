"""HTTP client for network-based device communication (PrusaLink, etc.)."""

import httpx


class HTTPClient:
    """Thin HTTP wrapper for local network devices.

    Handles base URL, auth headers, timeouts, and structured error returns.
    All methods return dicts — either the parsed JSON response or
    {"error": "reason"} on failure, matching Wallee's tool return convention.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 10.0,
        headers: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-Api-Key"] = api_key
        if headers:
            self._headers.update(headers)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=self._headers,
            timeout=timeout,
        )

    def get(self, path: str) -> dict:
        """GET request. Returns parsed JSON or error dict."""
        try:
            resp = self._client.get(path)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            return {"error": f"GET {path} timed out"}
        except httpx.HTTPStatusError as e:
            return {"error": f"GET {path} returned {e.response.status_code}"}
        except httpx.HTTPError as e:
            return {"error": f"GET {path} failed: {e}"}
        except Exception as e:
            return {"error": f"GET {path} unexpected error: {e}"}

    def put(self, path: str, json_body: dict | None = None) -> dict:
        """PUT request. Returns parsed JSON or error dict."""
        try:
            resp = self._client.put(path, json=json_body)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/json"):
                return resp.json()
            return {"status": "success", "status_code": resp.status_code}
        except httpx.TimeoutException:
            return {"error": f"PUT {path} timed out"}
        except httpx.HTTPStatusError as e:
            return {"error": f"PUT {path} returned {e.response.status_code}"}
        except httpx.HTTPError as e:
            return {"error": f"PUT {path} failed: {e}"}
        except Exception as e:
            return {"error": f"PUT {path} unexpected error: {e}"}

    def post(self, path: str, json_body: dict | None = None) -> dict:
        """POST request. Returns parsed JSON or error dict."""
        try:
            resp = self._client.post(path, json=json_body)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/json"):
                return resp.json()
            return {"status": "success", "status_code": resp.status_code}
        except httpx.TimeoutException:
            return {"error": f"POST {path} timed out"}
        except httpx.HTTPStatusError as e:
            return {"error": f"POST {path} returned {e.response.status_code}"}
        except httpx.HTTPError as e:
            return {"error": f"POST {path} failed: {e}"}
        except Exception as e:
            return {"error": f"POST {path} unexpected error: {e}"}

    def delete(self, path: str) -> dict:
        """DELETE request. Returns success dict or error dict."""
        try:
            resp = self._client.delete(path)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/json"):
                return resp.json()
            return {"status": "success", "status_code": resp.status_code}
        except httpx.TimeoutException:
            return {"error": f"DELETE {path} timed out"}
        except httpx.HTTPStatusError as e:
            return {"error": f"DELETE {path} returned {e.response.status_code}"}
        except httpx.HTTPError as e:
            return {"error": f"DELETE {path} failed: {e}"}
        except Exception as e:
            return {"error": f"DELETE {path} unexpected error: {e}"}

    def close(self):
        """Close the underlying HTTP client."""
        self._client.close()
