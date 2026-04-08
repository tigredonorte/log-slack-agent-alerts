# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator Agent entry point for the EKS Log Alerts pattern.

Registers with AgentCore Runtime via ``BedrockAgentCoreApp`` and dispatches
incoming invocations to the appropriate sub-agent based on the ``action``
field in the payload.  Supports four actions:

- ``poll_logs``    — calls Log_Ingestion_Agent to poll CloudWatch for errors
- ``classify``     — calls Classification_Agent to classify a single error
- ``notify``       — calls Notification_Agent to send a Slack alert
- ``run_pipeline`` — runs the full pipeline: poll → classify each → notify critical

The ``OrchestratorAgent`` class encapsulates the pipeline logic and trace
recording.  The ``@app.entrypoint`` handler creates an ``OrchestratorAgent``
and delegates to it.

Every sub-agent invocation is wrapped in a trace record containing:
agent name, action, input, output, data sources accessed, timestamp
(epoch seconds), and duration in milliseconds.

Validates: Requirements 1.5, 2.5, 6.1, 6.2, 6.3
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from agents.classification_agent import ClassificationAgent
from agents.log_ingestion_agent import LogIngestionAgent
from agents.notification_agent import NotificationAgent
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from config import AppConfig, load_config
from models import ClassificationResult, ExtractedError

logger: logging.Logger = logging.getLogger(__name__)

app: BedrockAgentCoreApp = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Tracing helpers
# ---------------------------------------------------------------------------


def _safe_serialise(*, value: Any) -> Any:
    """Convert a value to a JSON-safe representation.

    Pydantic models are converted via ``.model_dump()``, other objects
    fall back to ``str()``.

    Args:
        value: The value to serialise.

    Returns:
        A JSON-serialisable representation of the value.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_safe_serialise(value=item) for item in value]
    if isinstance(value, dict):
        return {k: _safe_serialise(value=v) for k, v in value.items()}
    # Pydantic models expose .model_dump()
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


# ---------------------------------------------------------------------------
# Gateway MCP client factory (mirrors strands-single-agent/tools/gateway.py)
# ---------------------------------------------------------------------------


def _create_mcp_tool_caller() -> Callable[..., Dict[str, Any]]:
    """Create an MCP tool caller backed by the AgentCore Gateway.

    Follows the same OAuth2 authentication pattern used in
    ``patterns/strands-single-agent/tools/gateway.py``.  The returned
    callable accepts ``tool_name`` (str) and ``arguments`` (dict) as
    keyword arguments and returns the Gateway response dict.

    The Gateway URL is resolved from SSM Parameter Store using the
    ``STACK_NAME`` environment variable.

    Returns:
        A callable conforming to the ``MCPToolCaller`` protocol expected
        by ``NotificationAgent``.

    Raises:
        ValueError: If ``STACK_NAME`` or ``GATEWAY_CREDENTIAL_PROVIDER_NAME``
            environment variables are not set.
    """
    # Import here to avoid hard failures when running unit tests that
    # do not have the AgentCore SDK installed.
    from bedrock_agentcore.identity.auth import requires_access_token  # noqa: WPS433
    from mcp.client.streamable_http import streamablehttp_client  # noqa: WPS433
    from strands.tools.mcp import MCPClient  # noqa: WPS433
    from utils.ssm import get_ssm_parameter  # noqa: WPS433

    stack_name: str = os.environ.get("STACK_NAME", "")
    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")

    gateway_credential_provider: str = os.environ.get(
        "GATEWAY_CREDENTIAL_PROVIDER_NAME", ""
    )
    if not gateway_credential_provider:
        raise ValueError(
            "GATEWAY_CREDENTIAL_PROVIDER_NAME environment variable is required"
        )

    gateway_url: str = get_ssm_parameter(f"/{stack_name}/gateway_url")

    @requires_access_token(
        provider_name=gateway_credential_provider,
        auth_flow="M2M",
        scopes=[],
    )
    def _fetch_gateway_token(access_token: str) -> str:
        """Fetch OAuth2 token for Gateway authentication.

        The ``@requires_access_token`` decorator handles token retrieval
        and refresh automatically.  Must be synchronous.

        Args:
            access_token: The OAuth2 access token injected by the
                decorator.

        Returns:
            The access token string.
        """
        return access_token

    mcp_client: MCPClient = MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url,
            headers={"Authorization": f"Bearer {_fetch_gateway_token()}"},
        ),
        prefix="gateway",
    )

    def _call_tool(*, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke an MCP tool through the AgentCore Gateway.

        Args:
            tool_name: The registered name of the Gateway tool.
            arguments: Keyword arguments matching the tool's input schema.

        Returns:
            The Gateway response dict with ``content`` or ``error``.
        """
        return mcp_client.call_tool(
            name=tool_name,
            arguments=arguments,
        )

    return _call_tool


# ---------------------------------------------------------------------------
# OrchestratorAgent class
# ---------------------------------------------------------------------------


class OrchestratorAgent:
    """Coordinates the EKS log monitoring multi-agent pipeline.

    Initialises the Log_Ingestion_Agent and Classification_Agent with
    parameters from the application configuration, then exposes a
    ``run_pipeline`` method that executes the full ingest-classify-notify
    workflow.  Optionally accepts an MCP tool caller for sending Slack
    notifications via the Notification_Agent.

    Args:
        config: Application configuration. If ``None``, configuration is
            loaded from environment variables via ``load_config()``.
        mcp_tool_caller: Optional callable for invoking MCP tools through
            the AgentCore Gateway. If provided, critical errors will be
            sent to Slack via the Notification_Agent.

    Attributes:
        config: The immutable application configuration.
        ingestion_agent: The Log_Ingestion_Agent instance.
        classification_agent: The Classification_Agent instance.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        mcp_tool_caller: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        """Initialise the OrchestratorAgent.

        Args:
            config: Application configuration dataclass. When omitted,
                ``load_config()`` reads required environment variables.
            mcp_tool_caller: Optional callable for invoking MCP tools
                through the AgentCore Gateway. If not provided,
                notification steps will count critical errors but skip
                the actual Slack dispatch.
        """
        if config is None:
            config = load_config()
        self.config: AppConfig = config
        self._mcp_tool_caller: Optional[Callable[..., Dict[str, Any]]] = mcp_tool_caller
        self._traces: List[Dict[str, Any]] = []

        # Initialise sub-agents with configuration values.
        self.ingestion_agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=config.monitored_log_groups,
            poll_interval_seconds=config.log_poll_interval_seconds,
        )
        self.classification_agent: ClassificationAgent = ClassificationAgent(
            model_id=config.classification_model_id,
            confidence_threshold=config.confidence_threshold,
            severity_examples_table_name=config.severity_examples_table_name,
        )

    # ------------------------------------------------------------------
    # Trace recording
    # ------------------------------------------------------------------

    def _record_trace(
        self,
        agent_name: str,
        action: str,
        input_payload: Any,
        output_payload: Any,
        data_sources: List[str],
        duration_ms: float,
    ) -> None:
        """Record a trace entry for pipeline observability.

        Each trace captures who did what, with which inputs and outputs,
        which external data sources were consulted, and how long it took.

        Args:
            agent_name: Logical name of the sub-agent (e.g.
                ``"Log_Ingestion_Agent"``).
            action: The method or operation performed (e.g.
                ``"poll_and_extract"``).
            input_payload: Serialisable representation of the action input.
            output_payload: Serialisable representation of the action output.
            data_sources: External systems consulted during the action
                (e.g. ``["CloudWatch Logs"]``).
            duration_ms: Wall-clock duration of the action in milliseconds.
        """
        self._traces.append(
            {
                "agent_name": agent_name,
                "action": action,
                "input": _safe_serialise(value=input_payload),
                "output": _safe_serialise(value=output_payload),
                "data_sources": data_sources,
                "timestamp": time.time(),
                "duration_ms": duration_ms,
            }
        )

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    def run_pipeline(self) -> Dict[str, Any]:
        """Execute the full pipeline: ingest → classify → notify.

        Steps:
            1. Poll CloudWatch log groups for new error events.
            2. Classify each extracted error by severity.
            3. Count critical-and-classified errors as notification
               candidates. If an MCP tool caller is available, actually
               dispatch Slack notifications via the Notification_Agent.

        Returns:
            A dict containing:
            - ``errors_detected``: number of errors found.
            - ``classifications``: list of error/classification pairs.
            - ``notifications_sent``: count of critical classified errors.
            - ``traces``: ordered list of trace records.
        """
        # Reset traces for this pipeline run.
        self._traces = []

        results: Dict[str, Any] = {
            "errors_detected": 0,
            "classifications": [],
            "notifications_sent": 0,
            "traces": [],
        }

        # --- Step 1: Ingest -----------------------------------------------
        start: float = time.time()
        errors: List[ExtractedError] = self.ingestion_agent.poll_and_extract()
        duration: float = (time.time() - start) * 1000

        self._record_trace(
            agent_name="Log_Ingestion_Agent",
            action="poll_and_extract",
            input_payload={
                "log_groups": self.config.monitored_log_groups,
            },
            output_payload={"errors_found": len(errors)},
            data_sources=["CloudWatch Logs"],
            duration_ms=duration,
        )
        results["errors_detected"] = len(errors)

        logger.info(
            "Ingestion complete: %d error(s) detected across %d log group(s).",
            len(errors),
            len(self.config.monitored_log_groups),
        )

        # --- Step 2: Classify each error ----------------------------------
        for error in errors:
            start = time.time()
            classification: ClassificationResult = self.classification_agent.classify(
                error
            )
            duration = (time.time() - start) * 1000

            self._record_trace(
                agent_name="Classification_Agent",
                action="classify",
                input_payload=error.model_dump(),
                output_payload=classification.model_dump(),
                data_sources=["Severity_Examples_Store", "Bedrock"],
                duration_ms=duration,
            )

            results["classifications"].append(
                {
                    "error": error.model_dump(),
                    "classification": classification.model_dump(),
                }
            )

            # --- Step 3: Count and optionally send critical notifications --
            if (
                classification.severity == "critical"
                and classification.status == "classified"
            ):
                results["notifications_sent"] += 1
                logger.info(
                    "Critical error detected in '%s' — notification queued.",
                    error.application_name,
                )

                # If we have an MCP tool caller, dispatch the notification.
                if self._mcp_tool_caller is not None:
                    notify_start: float = time.time()
                    notification_agent: NotificationAgent = NotificationAgent(
                        mcp_tool_caller=self._mcp_tool_caller,
                    )
                    sent: bool = notification_agent.notify(
                        error=error,
                        classification=classification,
                    )
                    notify_duration: float = (time.time() - notify_start) * 1000

                    self._record_trace(
                        agent_name="Notification_Agent",
                        action="notify",
                        input_payload={
                            "error_data": error.model_dump(),
                            "classification_data": classification.model_dump(),
                        },
                        output_payload={"notification_sent": sent},
                        data_sources=["Slack Webhook"],
                        duration_ms=notify_duration,
                    )

        results["traces"] = self._traces
        return results

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_traces(self) -> List[Dict[str, Any]]:
        """Return all trace records recorded during pipeline execution.

        Returns:
            An ordered list of trace record dicts, each containing
            agent_name, action, input, output, data_sources, timestamp,
            and duration_ms.
        """
        return self._traces


# ---------------------------------------------------------------------------
# AgentCore Runtime entrypoint
# ---------------------------------------------------------------------------


@app.entrypoint
async def invocations(payload: Dict[str, Any], context: RequestContext):
    """Main entrypoint — called by AgentCore Runtime on each request.

    Dispatches to the appropriate sub-agent based on the ``action`` field
    in the payload.  Supported actions:

    - ``poll_logs``    — poll CloudWatch for new error events
    - ``classify``     — classify a single error (requires ``error_data``)
    - ``notify``       — send a Slack notification (requires ``error_data``
      and ``classification_data``)
    - ``run_pipeline`` — run the full ingest → classify → notify pipeline

    Every invocation emits trace records capturing agent name, action,
    input/output, data sources, timestamp (epoch seconds), and duration
    in milliseconds.

    Args:
        payload: The request payload dict. Must contain an ``action``
            key. Additional keys depend on the action:
            - ``classify``: requires ``error_data`` (dict)
            - ``notify``: requires ``error_data`` (dict) and
              ``classification_data`` (dict)
        context: The AgentCore ``RequestContext`` for this invocation.

    Yields:
        A single response dict containing the action result, or an error
        dict if the action is unknown or required fields are missing.
    """
    action: str = payload.get("action", "")

    if not action:
        yield {
            "status": "error",
            "error": "Missing required field: 'action'. "
            "Supported actions: poll_logs, classify, notify, run_pipeline.",
        }
        return

    logger.info("Orchestrator received action='%s'.", action)

    # Load configuration once per invocation.
    config: AppConfig = load_config()

    if action == "poll_logs":
        orchestrator: OrchestratorAgent = OrchestratorAgent(config)
        start: float = time.time()
        errors: List[ExtractedError] = orchestrator.ingestion_agent.poll_and_extract()
        duration_ms: float = (time.time() - start) * 1000.0

        orchestrator._record_trace(
            agent_name="Log_Ingestion_Agent",
            action="poll_and_extract",
            input_payload={"monitored_log_groups": config.monitored_log_groups},
            output_payload={"errors_found": len(errors)},
            data_sources=["CloudWatch Logs"],
            duration_ms=duration_ms,
        )

        yield {
            "status": "success",
            "errors": [e.model_dump() for e in errors],
            "trace": orchestrator.get_traces(),
        }
        return

    if action == "classify":
        error_data: Optional[Dict[str, Any]] = payload.get("error_data")
        if error_data is None:
            yield {
                "status": "error",
                "error": "Action 'classify' requires 'error_data' in the payload.",
            }
            return

        start = time.time()
        error: ExtractedError = ExtractedError(**error_data)
        classification_agent: ClassificationAgent = ClassificationAgent(
            model_id=config.classification_model_id,
            confidence_threshold=config.confidence_threshold,
            severity_examples_table_name=config.severity_examples_table_name,
        )
        result: ClassificationResult = classification_agent.classify(error=error)
        duration_ms = (time.time() - start) * 1000.0

        yield {
            "status": "success",
            "classification": result.model_dump(),
        }
        return

    if action == "notify":
        error_data = payload.get("error_data")
        classification_data: Optional[Dict[str, Any]] = payload.get(
            "classification_data"
        )
        if error_data is None or classification_data is None:
            yield {
                "status": "error",
                "error": "Action 'notify' requires 'error_data' and "
                "'classification_data' in the payload.",
            }
            return

        mcp_tool_caller: Callable[..., Dict[str, Any]] = _create_mcp_tool_caller()
        error = ExtractedError(**error_data)
        classification: ClassificationResult = ClassificationResult(
            **classification_data
        )
        notification_agent: NotificationAgent = NotificationAgent(
            mcp_tool_caller=mcp_tool_caller,
        )
        sent: bool = notification_agent.notify(
            error=error,
            classification=classification,
        )

        yield {
            "status": "success",
            "notification_sent": sent,
        }
        return

    if action == "run_pipeline":
        mcp_tool_caller = _create_mcp_tool_caller()
        orchestrator = OrchestratorAgent(
            config,
            mcp_tool_caller=mcp_tool_caller,
        )
        pipeline_result: Dict[str, Any] = orchestrator.run_pipeline()
        pipeline_result["status"] = "success"
        yield pipeline_result
        return

    # Unknown action — fail loudly.
    yield {
        "status": "error",
        "error": f"Unknown action '{action}'. "
        "Supported actions: poll_logs, classify, notify, run_pipeline.",
    }


if __name__ == "__main__":
    app.run()
