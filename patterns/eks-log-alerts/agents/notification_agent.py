# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Notification Agent for the EKS Log Alerts pattern.

Sends formatted Slack Block Kit messages for critical errors via the
AgentCore Gateway MCP ``slack_webhook_tool``.  Constructs a message
payload containing all six required fields (severity, application name,
timestamp, error log, CloudWatch link, rationale) and delegates the
actual HTTP POST to the Gateway tool.

If the tool call fails, the agent logs a critical operational error and
returns ``False`` so the orchestrator can record the failure in the
trace store.

Validates: Requirements 3.1, 3.2, 3.4
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Protocol
from urllib.parse import quote

from models import ClassificationResult, ExtractedError, SlackMessagePayload

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CloudWatch console URL template
# ---------------------------------------------------------------------------
# The log group name is URL-encoded (with '/' encoded as '%2F') so the
# deep-link opens the correct log group in the AWS console.
CLOUDWATCH_LOG_GROUP_URL_TEMPLATE: str = (
    "https://console.aws.amazon.com/cloudwatch/home"
    "#logsV2:log-groups/log-group/{encoded_log_group_name}"
)


class MCPToolCaller(Protocol):
    """Protocol describing the callable used to invoke an MCP tool.

    Implementations must accept the tool name and a keyword-argument
    dict of tool parameters, and return a dict containing either a
    ``content`` key (on success) or an ``error`` key (on failure).
    """

    def __call__(self, *, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke an MCP tool through the AgentCore Gateway.

        Args:
            tool_name: The registered name of the Gateway tool to call
                (e.g. ``"slack_webhook_tool"``).
            arguments: A dict of keyword arguments matching the tool's
                input schema.

        Returns:
            A dict with either ``content`` (list of result blocks) on
            success, or ``error`` (str) on failure.
        """
        ...  # pragma: no cover


def build_cloudwatch_link(log_group_name: str) -> str:
    """Build a CloudWatch console deep-link URL for a log group.

    The log group name is URL-encoded so that characters like ``/`` are
    properly escaped for the console URL fragment.

    Args:
        log_group_name: The full CloudWatch log group name,
            e.g. ``"/aws/eks/team5-app/containers"``.

    Returns:
        A fully-formed CloudWatch console URL string pointing to the
        log group's log viewer.
    """
    # quote() with safe="" ensures '/' is encoded as '%2F', which the
    # CloudWatch console URL fragment requires.
    encoded_name: str = quote(log_group_name, safe="")
    return CLOUDWATCH_LOG_GROUP_URL_TEMPLATE.format(
        encoded_log_group_name=encoded_name,
    )


def build_slack_payload(
    *,
    error: ExtractedError,
    classification: ClassificationResult,
) -> SlackMessagePayload:
    """Construct a SlackMessagePayload from an error and its classification.

    Populates all six required fields for the Slack notification:
    severity, application name, timestamp, error message, CloudWatch
    log group link, and classification rationale.

    Args:
        error: The extracted error entry from CloudWatch logs.
        classification: The severity classification result for the error.

    Returns:
        A validated ``SlackMessagePayload`` instance containing all
        required notification fields.
    """
    log_group_link: str = build_cloudwatch_link(
        log_group_name=error.log_group_name,
    )

    return SlackMessagePayload(
        severity=classification.severity,
        application_name=error.application_name,
        timestamp=error.timestamp,
        error_message=error.error_message,
        log_group_link=log_group_link,
        rationale=classification.rationale,
    )


class NotificationAgent:
    """Agent that sends Slack notifications for critical errors.

    Constructs a Slack Block Kit message payload from an ``ExtractedError``
    and ``ClassificationResult``, then calls the ``slack_webhook_tool``
    via the AgentCore Gateway MCP session.

    The MCP tool caller is injected through the constructor so that the
    agent can be tested without a live Gateway connection.

    Args:
        mcp_tool_caller: A callable conforming to the ``MCPToolCaller``
            protocol. It is invoked with ``tool_name`` and ``arguments``
            keyword arguments and must return a dict with either
            ``content`` (success) or ``error`` (failure).

    Attributes:
        _mcp_tool_caller: The injected MCP tool caller used to invoke
            the ``slack_webhook_tool``.
    """

    def __init__(self, *, mcp_tool_caller: Callable[..., Dict[str, Any]]) -> None:
        """Initialise the NotificationAgent.

        Args:
            mcp_tool_caller: A callable that invokes MCP tools through
                the AgentCore Gateway. Must accept ``tool_name`` (str)
                and ``arguments`` (dict) as keyword arguments and return
                a dict with ``content`` or ``error``.
        """
        self._mcp_tool_caller: Callable[..., Dict[str, Any]] = mcp_tool_caller

    def notify(
        self,
        error: ExtractedError,
        classification: ClassificationResult,
    ) -> bool:
        """Send a Slack notification for a critical error.

        Builds the Slack message payload from the error and classification
        data, then calls the ``slack_webhook_tool`` via the injected MCP
        tool caller.

        If the tool call succeeds (response contains ``content``), returns
        ``True``.  If the tool call fails or raises an exception, logs a
        critical operational error and returns ``False``.

        Args:
            error: The extracted error entry from CloudWatch logs.
            classification: The severity classification result. Expected
                to have severity ``"critical"`` but the method does not
                enforce this — the caller is responsible for routing.

        Returns:
            ``True`` if the Slack notification was sent successfully,
            ``False`` if the notification failed for any reason.
        """
        # Step 1: Build the validated Slack message payload.
        payload: SlackMessagePayload = build_slack_payload(
            error=error,
            classification=classification,
        )

        # Step 2: Convert the payload to the dict format expected by the
        # slack_webhook_tool input schema.
        tool_arguments: Dict[str, Any] = {
            "severity": payload.severity,
            "application_name": payload.application_name,
            "timestamp": payload.timestamp,
            "error_message": payload.error_message,
            "log_group_link": payload.log_group_link,
            "rationale": payload.rationale,
        }

        # Step 3: Call the slack_webhook_tool via the MCP Gateway.
        try:
            response: Dict[str, Any] = self._mcp_tool_caller(
                tool_name="slack_webhook_tool",
                arguments=tool_arguments,
            )
        except Exception as exc:
            # The MCP call itself raised — log the critical error with
            # the full payload so operators can diagnose the failure.
            logger.critical(
                "Slack notification failed — MCP tool call raised an exception. "
                "Error: %s | Payload: %s",
                exc,
                tool_arguments,
            )
            return False

        # Step 4: Inspect the response for success or failure.
        if "error" in response:
            logger.critical(
                "Slack notification failed — tool returned an error. "
                "Error: %s | Payload: %s",
                response["error"],
                tool_arguments,
            )
            return False

        logger.info(
            "Slack notification sent successfully for %s alert on '%s' at %s.",
            classification.severity,
            error.application_name,
            error.timestamp,
        )
        return True
