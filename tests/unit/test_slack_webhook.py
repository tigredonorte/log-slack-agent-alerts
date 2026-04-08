# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Slack_Webhook_Tool Lambda retry logic.

Tests cover:
- Non-2xx response triggers retries with exponential backoff (1s, 2s, 4s)
- All retries exhausted raises RuntimeError with descriptive message
- Successful first attempt requires no retries
- Success on second attempt after initial failure
- Handler returns error dict when all retries fail

Validates: Requirements 3.3, 3.4
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import the Slack webhook Lambda module from the gateway directory.
# ---------------------------------------------------------------------------
_TOOL_DIR: Path = (
    Path(__file__).resolve().parents[2] / "gateway" / "tools" / "slack_webhook"
)

if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

_module = importlib.import_module("slack_webhook_lambda")

_post_to_slack = _module._post_to_slack
_build_slack_blocks = _module._build_slack_blocks
handler = _module.handler
MAX_RETRIES = _module.MAX_RETRIES
BACKOFF_DELAYS = _module.BACKOFF_DELAYS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sample_payload() -> Dict[str, Any]:
    """Build a minimal Slack payload for testing.

    Returns:
        A dict representing a Slack webhook JSON payload.
    """
    return {
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "test"}}],
        "text": "CRITICAL alert for test-app at 2026-01-01T00:00:00Z: error",
    }


def _make_mock_response(*, status_code: int = 200) -> MagicMock:
    """Create a mock HTTP response with the given status code.

    Args:
        status_code: The HTTP status code the mock response should return.

    Returns:
        A MagicMock configured as a context-manager-compatible HTTP response.
    """
    mock_resp: MagicMock = MagicMock()
    mock_resp.getcode.return_value = status_code
    mock_resp.read.return_value = b"ok"
    # Support ``with urlopen(...) as response:`` pattern.
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_handler_event() -> Dict[str, Any]:
    """Build a sample Lambda event dict with all required fields.

    Returns:
        A dict matching the expected Slack webhook tool event shape.
    """
    return {
        "severity": "critical",
        "application_name": "team5-checkout",
        "timestamp": "2026-04-08T10:00:00Z",
        "error_message": "NullPointerException in OrderService.processPayment",
        "log_group_link": "https://console.aws.amazon.com/cloudwatch/home#logsV2:log-groups/log-group/team5-checkout",
        "rationale": "Unhandled exception in payment processing path causes order failures.",
    }


def _make_lambda_context(*, tool_name: str = "slack_webhook_tool") -> MagicMock:
    """Build a mock Lambda context with AgentCore metadata.

    Args:
        tool_name: The tool name suffix after the ``___`` delimiter.

    Returns:
        A MagicMock configured as a Lambda context object.
    """
    ctx: MagicMock = MagicMock()
    ctx.client_context.custom = {
        "bedrockAgentCoreToolName": f"prefix___{tool_name}",
    }
    return ctx


# ---------------------------------------------------------------------------
# Tests: _post_to_slack retry logic (Requirements 3.3, 3.4)
# ---------------------------------------------------------------------------


class TestPostToSlackRetry:
    """Verify that _post_to_slack retries on failure with exponential backoff
    and raises RuntimeError when all retries are exhausted."""

    @patch("slack_webhook_lambda.time.sleep", return_value=None)
    @patch("slack_webhook_lambda.urllib.request.urlopen")
    def test_non_2xx_retries_three_times_with_backoff(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """A non-2xx response on every attempt should trigger exactly 3
        attempts with exponential backoff delays of 1s and 2s between them,
        then raise RuntimeError.

        Args:
            mock_urlopen: Patched urlopen returning non-2xx responses.
            mock_sleep: Patched time.sleep to verify backoff delays.
        """
        # All 3 attempts return HTTP 500.
        mock_urlopen.return_value = _make_mock_response(status_code=500)

        with pytest.raises(RuntimeError, match=r"All 3 Slack webhook attempts failed"):
            _post_to_slack(
                webhook_url="https://hooks.slack.com/test",
                payload=_make_sample_payload(),
            )

        # urlopen should be called exactly MAX_RETRIES times.
        assert mock_urlopen.call_count == MAX_RETRIES

        # Backoff sleeps happen between attempts (not after the last one).
        # Delays: 1.0s after attempt 1, 2.0s after attempt 2.
        expected_sleep_calls: List[call] = [
            call(BACKOFF_DELAYS[0]),  # 1.0
            call(BACKOFF_DELAYS[1]),  # 2.0
        ]
        assert mock_sleep.call_args_list == expected_sleep_calls

    @patch("slack_webhook_lambda.time.sleep", return_value=None)
    @patch("slack_webhook_lambda.urllib.request.urlopen")
    def test_all_retries_fail_raises_runtime_error_with_payload_info(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When all retry attempts fail, a RuntimeError is raised and each
        failed attempt is logged as a warning.

        Args:
            mock_urlopen: Patched urlopen raising URLError on every call.
            mock_sleep: Patched time.sleep to avoid real delays.
            caplog: Pytest fixture for capturing log output.
        """
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match=r"All 3 Slack webhook attempts failed"):
                _post_to_slack(
                    webhook_url="https://hooks.slack.com/test",
                    payload=_make_sample_payload(),
                )

        # Each failed attempt should produce a warning log.
        warning_messages: List[str] = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_messages) == MAX_RETRIES, (
            f"Expected {MAX_RETRIES} warning logs, got {len(warning_messages)}: "
            f"{warning_messages}"
        )

    @patch("slack_webhook_lambda.time.sleep", return_value=None)
    @patch("slack_webhook_lambda.urllib.request.urlopen")
    def test_success_on_first_attempt_no_retries(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """A 200 response on the first attempt should succeed immediately
        with no retries and no sleep calls.

        Args:
            mock_urlopen: Patched urlopen returning HTTP 200.
            mock_sleep: Patched time.sleep — should not be called.
        """
        mock_urlopen.return_value = _make_mock_response(status_code=200)

        # Should not raise.
        _post_to_slack(
            webhook_url="https://hooks.slack.com/test",
            payload=_make_sample_payload(),
        )

        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("slack_webhook_lambda.time.sleep", return_value=None)
    @patch("slack_webhook_lambda.urllib.request.urlopen")
    def test_success_on_second_attempt_after_first_failure(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """If the first attempt fails but the second succeeds, only one
        retry and one sleep call should occur.

        Args:
            mock_urlopen: Patched urlopen — first call fails, second succeeds.
            mock_sleep: Patched time.sleep — should be called once with 1.0s.
        """
        import urllib.error

        # First call raises, second call succeeds.
        mock_urlopen.side_effect = [
            urllib.error.URLError("Temporary failure"),
            _make_mock_response(status_code=200),
        ]

        _post_to_slack(
            webhook_url="https://hooks.slack.com/test",
            payload=_make_sample_payload(),
        )

        assert mock_urlopen.call_count == 2
        # Only one backoff sleep between attempt 1 and attempt 2.
        mock_sleep.assert_called_once_with(BACKOFF_DELAYS[0])


# ---------------------------------------------------------------------------
# Tests: handler integration with retry logic (Requirements 3.3, 3.4)
# ---------------------------------------------------------------------------


class TestHandlerRetryIntegration:
    """Verify that the Lambda handler returns an error dict when all
    Slack retries are exhausted."""

    @patch.dict("os.environ", {"SLACK_CHANNEL_WEBHOOK_URL": "https://hooks.slack.com/test"})
    @patch("slack_webhook_lambda.time.sleep", return_value=None)
    @patch("slack_webhook_lambda.urllib.request.urlopen")
    def test_handler_returns_error_when_all_retries_fail(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """The handler should catch the RuntimeError from _post_to_slack
        and return an error dict instead of raising.

        Args:
            mock_urlopen: Patched urlopen returning HTTP 500 on every call.
            mock_sleep: Patched time.sleep to avoid real delays.
        """
        mock_urlopen.return_value = _make_mock_response(status_code=500)

        result: Dict[str, Any] = handler(
            event=_make_handler_event(),
            context=_make_lambda_context(),
        )

        assert "error" in result, f"Expected 'error' key in result, got: {result}"
        assert "Internal server error" in result["error"]

    @patch.dict("os.environ", {"SLACK_CHANNEL_WEBHOOK_URL": "https://hooks.slack.com/test"})
    @patch("slack_webhook_lambda.time.sleep", return_value=None)
    @patch("slack_webhook_lambda.urllib.request.urlopen")
    def test_handler_returns_success_on_first_attempt(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """The handler should return a success content dict when the Slack
        POST succeeds on the first attempt.

        Args:
            mock_urlopen: Patched urlopen returning HTTP 200.
            mock_sleep: Patched time.sleep — should not be called.
        """
        mock_urlopen.return_value = _make_mock_response(status_code=200)

        result: Dict[str, Any] = handler(
            event=_make_handler_event(),
            context=_make_lambda_context(),
        )

        assert "content" in result, f"Expected 'content' key in result, got: {result}"
        assert result["content"][0]["type"] == "text"
        assert "successfully" in result["content"][0]["text"].lower()
        mock_sleep.assert_not_called()
