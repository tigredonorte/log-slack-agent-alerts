# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Slack Webhook Tool Lambda for FAST AgentCore Gateway.

Posts formatted Slack Block Kit messages to a Slack incoming webhook URL.
Follows the FAST "one tool per Lambda" pattern (see sample_tool_lambda.py).

The Lambda reads the webhook URL from the ``SLACK_CHANNEL_WEBHOOK_URL``
environment variable.  If the variable is missing the handler fails
immediately with a descriptive error — no silent fallback.

Retry logic: up to 3 retries with exponential back-off (1 s, 2 s, 4 s).
Each failed attempt is logged.  If all retries are exhausted the handler
raises an exception so the calling Notification_Agent can record the
critical operational error.

Validates: Requirements 3.1, 3.3, 3.4
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
MAX_RETRIES: int = 3
# Back-off delays in seconds for each retry attempt (1 s, 2 s, 4 s)
BACKOFF_DELAYS: list[float] = [1.0, 2.0, 4.0]


# ---------------------------------------------------------------------------
# Severity → emoji mapping for Slack Block Kit messages
# ---------------------------------------------------------------------------
SEVERITY_EMOJI: dict[str, str] = {
    "low": "🟡",
    "medium": "🟠",
    "high": "🔴",
    "critical": "🚨",
}


def _build_slack_blocks(
    *,
    severity: str,
    application_name: str,
    timestamp: str,
    error_message: str,
    log_group_link: str,
    rationale: str,
) -> list[dict[str, Any]]:
    """Build a Slack Block Kit payload containing all six required fields.

    Args:
        severity: Error severity level (low, medium, high, critical).
        application_name: Name of the application that produced the error.
        timestamp: ISO 8601 timestamp of the error event.
        error_message: Full error log text.
        log_group_link: URL deep-link to the CloudWatch log group.
        rationale: Classification rationale explaining the severity.

    Returns:
        A list of Slack Block Kit block dicts ready for the ``blocks`` field
        of a Slack webhook payload.
    """
    emoji = SEVERITY_EMOJI.get(severity.lower(), "⚪")

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {severity.upper()} Alert — {application_name}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Severity:*\n{severity}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Application:*\n{application_name}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Timestamp:*\n{timestamp}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*CloudWatch Logs:*\n<{log_group_link}|View Logs>",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Error Message:*\n```{error_message}```",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Rationale:*\n{rationale}",
            },
        },
        {"type": "divider"},
    ]

    return blocks


def _post_to_slack(*, webhook_url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload to the Slack webhook URL with retry logic.

    Retries up to ``MAX_RETRIES`` times with exponential back-off
    (delays defined in ``BACKOFF_DELAYS``).  Each failed attempt is logged.
    If all retries are exhausted an exception is raised so the calling
    Notification_Agent can record the critical operational error.

    Args:
        webhook_url: The Slack incoming webhook URL.
        payload: The JSON-serialisable Slack message payload.

    Raises:
        RuntimeError: If all retry attempts fail.
    """
    encoded_payload: bytes = json.dumps(payload).encode("utf-8")

    # Build the request once — it can be reused across retries.
    request = urllib.request.Request(
        url=webhook_url,
        data=encoded_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_exception: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request) as response:
                status_code: int = response.getcode()
                if 200 <= status_code <= 299:
                    logger.info(
                        "Slack webhook POST succeeded on attempt %d with status %d",
                        attempt,
                        status_code,
                    )
                    return
                # Non-2xx that didn't raise — treat as failure
                body = response.read().decode("utf-8", errors="replace")
                last_exception = RuntimeError(
                    f"Slack returned non-2xx status {status_code}: {body}"
                )
                logger.warning(
                    "Slack webhook attempt %d/%d failed — HTTP %d: %s",
                    attempt,
                    MAX_RETRIES,
                    status_code,
                    body,
                )
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last_exception = exc
            logger.warning(
                "Slack webhook attempt %d/%d failed — %s: %s",
                attempt,
                MAX_RETRIES,
                type(exc).__name__,
                exc,
            )

        # Sleep with exponential back-off before the next retry
        if attempt < MAX_RETRIES:
            delay = BACKOFF_DELAYS[attempt - 1]
            logger.info("Retrying in %.1f seconds …", delay)
            time.sleep(delay)

    # All retries exhausted — raise so the caller can log the critical error
    raise RuntimeError(
        f"All {MAX_RETRIES} Slack webhook attempts failed. "
        f"Last error: {last_exception}"
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Slack Webhook Tool Lambda handler for FAST AgentCore Gateway.

    DESIGN PATTERN:
    This Lambda follows the "one tool per Lambda" design pattern where each
    Lambda function implements exactly one tool.  See ``sample_tool_lambda.py``
    for the canonical reference.

    INPUT FORMAT:
        event  — contains tool arguments directly (not wrapped in HTTP body).
        context.client_context.custom['bedrockAgentCoreToolName'] — full tool
        name with the target prefix (stripped using the ``___`` delimiter).

    OUTPUT FORMAT:
        Return ``{'content': [{'type': 'text', 'text': result}]}`` on success.
        Return ``{'error': message}`` on failure.

    Args:
        event: Tool arguments passed directly from the AgentCore Gateway.
        context: Lambda context with AgentCore metadata in
            ``client_context.custom``.

    Returns:
        A dict with either a ``content`` list (success) or an ``error``
        string (failure).
    """
    logger.info("Received event: %s", json.dumps(event))

    try:
        # ------------------------------------------------------------------
        # 1. Extract tool name from Lambda context (FAST pattern)
        # ------------------------------------------------------------------
        delimiter = "___"
        original_tool_name: str = context.client_context.custom[
            "bedrockAgentCoreToolName"
        ]
        tool_name: str = original_tool_name[
            original_tool_name.index(delimiter) + len(delimiter) :
        ]
        logger.info("Processing tool: %s", tool_name)

        # ------------------------------------------------------------------
        # 2. Route to the single tool this Lambda supports
        # ------------------------------------------------------------------
        if tool_name != "slack_webhook_tool":
            logger.error("Unexpected tool name: %s", tool_name)
            return {
                "error": (
                    f"This Lambda only supports 'slack_webhook_tool', "
                    f"received: {tool_name}"
                )
            }

        # ------------------------------------------------------------------
        # 3. Read webhook URL from environment — fail loudly if missing
        # ------------------------------------------------------------------
        webhook_url: str = os.environ["SLACK_CHANNEL_WEBHOOK_URL"]

        # ------------------------------------------------------------------
        # 4. Extract required fields from the event
        # ------------------------------------------------------------------
        severity: str = event["severity"]
        application_name: str = event["application_name"]
        timestamp: str = event["timestamp"]
        error_message: str = event["error_message"]
        log_group_link: str = event["log_group_link"]
        rationale: str = event["rationale"]

        # ------------------------------------------------------------------
        # 5. Build Slack Block Kit payload
        # ------------------------------------------------------------------
        blocks = _build_slack_blocks(
            severity=severity,
            application_name=application_name,
            timestamp=timestamp,
            error_message=error_message,
            log_group_link=log_group_link,
            rationale=rationale,
        )

        slack_payload: dict[str, Any] = {
            "blocks": blocks,
            # Fallback text for notifications / clients that don't render blocks
            "text": (
                f"{severity.upper()} alert for {application_name} "
                f"at {timestamp}: {error_message[:200]}"
            ),
        }

        # ------------------------------------------------------------------
        # 6. POST to Slack with retry logic
        # ------------------------------------------------------------------
        _post_to_slack(webhook_url=webhook_url, payload=slack_payload)

        result_text = (
            f"Slack notification sent successfully for {severity} alert "
            f"on {application_name} at {timestamp}."
        )
        logger.info(result_text)

        return {"content": [{"type": "text", "text": result_text}]}

    except Exception as exc:
        logger.error("Error processing request: %s", exc, exc_info=True)
        return {"error": f"Internal server error: {exc}"}
