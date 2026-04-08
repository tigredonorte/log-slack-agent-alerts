# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Slack_Webhook_Tool Lambda — AgentCore Gateway Lambda target.

Posts formatted Slack Block Kit messages to an incoming webhook URL.
Receives six fields (severity, application_name, timestamp, error_message,
log_group_link, rationale) from the Notification_Agent via the AgentCore
Gateway MCP protocol, constructs a Block Kit payload, and POSTs it to the
Slack webhook endpoint.

Retry logic: up to 3 attempts with exponential backoff (1s, 2s, 4s).
Each failed attempt is logged as a WARNING.  If all attempts fail, a
RuntimeError is raised so the Notification_Agent can log the critical
operational error.

Validates: Requirements 3.1, 3.3, 3.4
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

logger: logging.Logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Retry configuration (Requirement 3.3)
# ---------------------------------------------------------------------------
MAX_RETRIES: int = 3
BACKOFF_DELAYS: List[float] = [1.0, 2.0, 4.0]


# ---------------------------------------------------------------------------
# Slack Block Kit message builder
# ---------------------------------------------------------------------------


def _build_slack_blocks(
    *,
    severity: str,
    application_name: str,
    timestamp: str,
    error_message: str,
    log_group_link: str,
    rationale: str,
) -> Dict[str, Any]:
    """Build a Slack Block Kit message payload from the six required fields.

    Constructs a rich-formatted Slack message with a header, severity badge,
    application details, error text, a link to CloudWatch logs, and the
    classification rationale.

    Args:
        severity: Error severity level (low, medium, high, critical).
        application_name: Name of the application that produced the error.
        timestamp: ISO 8601 timestamp of the error event.
        error_message: Full error log text.
        log_group_link: URL link to the CloudWatch log group.
        rationale: Classification rationale explaining the severity assignment.

    Returns:
        A dict representing the Slack Block Kit JSON payload, including
        both ``blocks`` (rich formatting) and ``text`` (fallback).
    """
    # Map severity to an emoji for visual distinction in Slack.
    severity_emoji: Dict[str, str] = {
        "low": ":large_blue_circle:",
        "medium": ":large_yellow_circle:",
        "high": ":large_orange_circle:",
        "critical": ":red_circle:",
    }
    emoji: str = severity_emoji.get(severity.lower(), ":warning:")

    blocks: List[Dict[str, Any]] = [
        # Header block
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {severity.upper()} Alert — {application_name}",
                "emoji": True,
            },
        },
        # Severity + timestamp section
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Severity:*\n{severity.upper()}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Timestamp:*\n{timestamp}",
                },
            ],
        },
        # Application name section
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Application:*\n{application_name}",
                },
            ],
        },
        # Error message section
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Error Message:*\n```{error_message}```",
            },
        },
        # Rationale section
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Classification Rationale:*\n{rationale}",
            },
        },
        # CloudWatch link section
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*CloudWatch Logs:*\n<{log_group_link}|View Log Group>",
            },
        },
        # Divider at the end
        {"type": "divider"},
    ]

    # The ``text`` field is a plain-text fallback for notifications and
    # clients that do not render Block Kit.
    fallback_text: str = (
        f"{severity.upper()} alert for {application_name} at {timestamp}: "
        f"{error_message}"
    )

    return {"blocks": blocks, "text": fallback_text}


# ---------------------------------------------------------------------------
# HTTP POST with retry logic (Requirement 3.3)
# ---------------------------------------------------------------------------


def _post_to_slack(
    *,
    webhook_url: str,
    payload: Dict[str, Any],
) -> None:
    """POST a JSON payload to a Slack incoming webhook URL with retries.

    Attempts the POST up to ``MAX_RETRIES`` times.  On each failure
    (non-2xx status or network error), a WARNING is logged and the
    function sleeps for an exponentially increasing delay before the
    next attempt.  Sleep only occurs *between* attempts — not after the
    final failed attempt.

    Args:
        webhook_url: The Slack incoming webhook URL to POST to.
        payload: The JSON-serialisable Slack message payload.

    Raises:
        RuntimeError: If all ``MAX_RETRIES`` attempts fail.  The error
            message includes the number of attempts for diagnostics.
    """
    encoded_payload: bytes = json.dumps(payload).encode("utf-8")

    request: urllib.request.Request = urllib.request.Request(
        url=webhook_url,
        data=encoded_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request) as response:
                status_code: int = response.getcode()
                if 200 <= status_code <= 299:
                    logger.info(
                        "Slack webhook POST succeeded on attempt %d (HTTP %d).",
                        attempt,
                        status_code,
                    )
                    return
                # Non-2xx — treat as a retryable failure.
                logger.warning(
                    "Slack webhook attempt %d/%d failed with HTTP %d.",
                    attempt,
                    MAX_RETRIES,
                    status_code,
                )
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            logger.warning(
                "Slack webhook attempt %d/%d failed with error: %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

        # Sleep between attempts, but not after the last failed attempt.
        if attempt < MAX_RETRIES:
            delay: float = BACKOFF_DELAYS[attempt - 1]
            logger.info("Retrying in %.1fs …", delay)
            time.sleep(delay)

    raise RuntimeError(
        f"All {MAX_RETRIES} Slack webhook attempts failed. "
        f"Webhook URL: {webhook_url}"
    )


# ---------------------------------------------------------------------------
# Lambda handler (FAST AgentCore Gateway pattern)
# ---------------------------------------------------------------------------


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Slack_Webhook_Tool Lambda handler for FAST AgentCore Gateway.

    Follows the FAST "one tool per Lambda" pattern.  Extracts the tool
    name from the Lambda context, reads the six required fields from the
    event, builds a Slack Block Kit payload, and POSTs it to the webhook
    URL specified by the ``SLACK_CHANNEL_WEBHOOK_URL`` environment variable.

    INPUT FORMAT:
        event: Contains tool arguments directly (severity, application_name,
            timestamp, error_message, log_group_link, rationale).
        context.client_context.custom['bedrockAgentCoreToolName']:
            Full tool name with target prefix (e.g.
            ``"slack_webhook_target___slack_webhook_tool"``).

    OUTPUT FORMAT:
        On success: ``{"content": [{"type": "text", "text": "..."}]}``
        On failure: ``{"error": "Internal server error: ..."}``

    Args:
        event: Tool arguments passed directly from the AgentCore Gateway.
        context: Lambda context with AgentCore metadata in
            ``client_context.custom``.

    Returns:
        A dict with ``content`` on success or ``error`` on failure.
    """
    logger.info("Received event: %s", json.dumps(event))

    try:
        # --- Extract tool name from context (FAST pattern) ----------------
        delimiter: str = "___"
        original_tool_name: str = context.client_context.custom[
            "bedrockAgentCoreToolName"
        ]
        if delimiter in original_tool_name:
            tool_name: str = original_tool_name[
                original_tool_name.index(delimiter) + len(delimiter) :
            ]
        else:
            tool_name = original_tool_name

        logger.info("Processing tool: %s", tool_name)

        if tool_name != "slack_webhook_tool":
            logger.error("Unexpected tool name: %s", tool_name)
            return {
                "error": (
                    f"This Lambda only supports 'slack_webhook_tool', "
                    f"received: {tool_name}"
                )
            }

        # --- Read required fields from event (fail loudly if missing) -----
        severity: str = event["severity"]
        application_name: str = event["application_name"]
        timestamp: str = event["timestamp"]
        error_message: str = event["error_message"]
        log_group_link: str = event["log_group_link"]
        rationale: str = event["rationale"]

        # --- Read webhook URL from environment (fail loudly if missing) ---
        webhook_url: str = os.environ["SLACK_CHANNEL_WEBHOOK_URL"]

        # --- Build Slack Block Kit payload --------------------------------
        slack_payload: Dict[str, Any] = _build_slack_blocks(
            severity=severity,
            application_name=application_name,
            timestamp=timestamp,
            error_message=error_message,
            log_group_link=log_group_link,
            rationale=rationale,
        )

        # --- POST to Slack with retry logic (Requirement 3.3) -------------
        _post_to_slack(webhook_url=webhook_url, payload=slack_payload)

        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Slack notification sent successfully for "
                        f"{severity.upper()} alert on '{application_name}' "
                        f"at {timestamp}."
                    ),
                }
            ]
        }

    except Exception as exc:
        logger.error("Error processing request: %s", str(exc), exc_info=True)
        return {"error": f"Internal server error: {str(exc)}"}
