"""Tests for LLM client (no real API calls)."""

import json
from unittest.mock import patch, MagicMock, call
import httpx
import pytest

from wallee.agent.llm_client import LLMClient, MAX_RETRIES


@pytest.fixture
def client():
    return LLMClient(api_key="test-key", model="test/model")


class TestLLMClient:
    def test_successful_call(self, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"type": "WAIT", "reason": "all good"}'}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("wallee.agent.llm_client.httpx.post", return_value=mock_response) as mock_post:
            result = client.call("system prompt here")

        assert result == '{"type": "WAIT", "reason": "all good"}'
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == "test/model"
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["content"] == "Decide your next action."
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "agent_decision"
        assert body["response_format"]["json_schema"]["strict"] is True

    def test_custom_messages(self, client):
        """Support vision content blocks via messages parameter."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        mock_response.raise_for_status = MagicMock()

        custom = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
                {"type": "text", "text": "look at this"},
            ]},
        ]

        with patch("wallee.agent.llm_client.httpx.post", return_value=mock_response) as mock_post:
            client.call("ignored", messages=custom)

        body = mock_post.call_args.kwargs["json"]
        assert body["messages"] == custom

    def test_uses_bearer_auth(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        mock_response.raise_for_status = MagicMock()

        with patch("wallee.agent.llm_client.httpx.post", return_value=mock_response) as mock_post:
            client.call("prompt")

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-key"


class TestRetryLogic:
    @patch("wallee.agent.llm_client.time.sleep")
    def test_retries_on_connect_error(self, mock_sleep, client):
        """DNS/connection errors trigger retries."""
        mock_ok = MagicMock()
        mock_ok.json.return_value = {"choices": [{"message": {"content": '{"type":"WAIT"}'}}]}
        mock_ok.raise_for_status = MagicMock()

        with patch("wallee.agent.llm_client.httpx.post",
                    side_effect=[httpx.ConnectError("DNS failed"), mock_ok]) as mock_post:
            result = client.call("prompt")

        assert result == '{"type":"WAIT"}'
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(2.0)  # backoff: 2 * attempt(1)

    @patch("wallee.agent.llm_client.time.sleep")
    def test_retries_on_connect_timeout(self, mock_sleep, client):
        mock_ok = MagicMock()
        mock_ok.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        mock_ok.raise_for_status = MagicMock()

        with patch("wallee.agent.llm_client.httpx.post",
                    side_effect=[httpx.ConnectTimeout("timeout"), mock_ok]):
            result = client.call("prompt")

        assert result == "{}"

    @patch("wallee.agent.llm_client.time.sleep")
    def test_retries_on_read_timeout(self, mock_sleep, client):
        mock_ok = MagicMock()
        mock_ok.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        mock_ok.raise_for_status = MagicMock()

        with patch("wallee.agent.llm_client.httpx.post",
                    side_effect=[httpx.ReadTimeout("read timeout"), mock_ok]):
            result = client.call("prompt")

        assert result == "{}"

    @patch("wallee.agent.llm_client.time.sleep")
    def test_exhausts_retries_returns_empty(self, mock_sleep, client):
        """After MAX_RETRIES failures, returns empty string."""
        with patch("wallee.agent.llm_client.httpx.post",
                    side_effect=httpx.ConnectError("DNS failed")):
            result = client.call("prompt")

        assert result == ""
        assert mock_sleep.call_count == MAX_RETRIES - 1  # sleep between retries

    def test_no_retry_on_http_4xx(self, client):
        """HTTP 429 (rate limit) is NOT retried."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "rate limited"
        error = httpx.HTTPStatusError("error", request=MagicMock(), response=mock_resp)

        with patch("wallee.agent.llm_client.httpx.post", side_effect=error) as mock_post:
            result = client.call("prompt")

        assert result == ""
        assert mock_post.call_count == 1  # no retry

    def test_no_retry_on_http_5xx(self, client):
        """HTTP 500 is NOT retried."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "internal error"
        error = httpx.HTTPStatusError("error", request=MagicMock(), response=mock_resp)

        with patch("wallee.agent.llm_client.httpx.post", side_effect=error) as mock_post:
            result = client.call("prompt")

        assert result == ""
        assert mock_post.call_count == 1

    @patch("wallee.agent.llm_client.time.sleep")
    def test_backoff_increases(self, mock_sleep, client):
        """Backoff should be 2s, 4s for attempts 1, 2."""
        with patch("wallee.agent.llm_client.httpx.post",
                    side_effect=httpx.ConnectError("DNS")):
            client.call("prompt")

        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(2.0)   # attempt 1: 2 * 1
        mock_sleep.assert_any_call(4.0)   # attempt 2: 2 * 2
