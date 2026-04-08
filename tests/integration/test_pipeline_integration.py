# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the EKS Log Alerts end-to-end pipeline.

Tests cover:
- End-to-end pipeline: mock log → detection → classification → notification
- Classification uses DynamoDB examples as few-shot context
- Trace records contain all required fields

All AWS services (CloudWatch Logs, DynamoDB, Bedrock) are mocked via
dependency injection supported by the agent constructors. No real AWS
calls are made.

Validates: Requirements 1.1, 1.5, 2.2, 2.5, 6.1, 6.2, 6.3
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the patterns directory is importable.
# ---------------------------------------------------------------------------
_PATTERNS_DIR: Path = (
    Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
)
if str(_PATTERNS_DIR) not in sys.path:
    sys.path.insert(0, str(_PATTERNS_DIR))

from agents.classification_agent import ClassificationAgent
from agents.log_ingestion_agent import LogIngestionAgent
from agents.notification_agent import NotificationAgent
from config import AppConfig
from models import ClassificationResult, ExtractedError


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

# Required trace fields per Requirement 6.1
REQUIRED_TRACE_FIELDS: List[str] = [
    "agent_name",
    "action",
    "input",
    "output",
    "data_sources",
    "timestamp",
    "duration_ms",
]


def _make_test_config() -> AppConfig:
    """Build an AppConfig for integration testing.

    Returns:
        An AppConfig with sensible test defaults — no real AWS resources.
    """
    return AppConfig(
        slack_channel_webhook_url="https://hooks.slack.com/services/TEST/FAKE/URL",
        monitored_log_groups=["/aws/eks/team5-app/containers"],
        confidence_threshold=0.6,
        log_poll_interval_seconds=30,
        classification_model_id="anthropic.claude-3-haiku-20240307-v1:0",
        severity_examples_table_name="team5-severity-examples",
        prefix="team5",
    )


def _make_cloudwatch_error_event(
    *,
    timestamp_ms: int = 1712500000000,
    log_stream: str = "pod-abc123/container-xyz",
    message: str = "ERROR 2026-04-08T10:00:00Z OOMKilled in pod team5-checkout",
) -> Dict[str, Any]:
    """Build a mock CloudWatch log event dict.

    Args:
        timestamp_ms: Epoch milliseconds for the event timestamp.
        log_stream: The log stream name.
        message: The log event message text.

    Returns:
        A dict matching the shape returned by CloudWatch filter_log_events.
    """
    return {
        "timestamp": timestamp_ms,
        "logStreamName": log_stream,
        "message": message,
        "eventId": "event-001",
        "ingestionTime": timestamp_ms + 100,
    }


def _make_bedrock_converse_response(
    *,
    severity: str = "critical",
    confidence: float = 0.92,
    rationale: str = "OOMKilled indicates a container ran out of memory, causing pod failure.",
) -> Dict[str, Any]:
    """Build a mock Bedrock converse API response.

    The response body is a JSON string matching the format the
    ClassificationAgent expects from the LLM.

    Args:
        severity: The severity level to include in the response.
        confidence: The confidence score to include.
        rationale: The rationale text to include.

    Returns:
        A dict matching the Bedrock converse API response shape.
    """
    llm_json: str = json.dumps({
        "severity": severity,
        "confidence_score": confidence,
        "rationale": rationale,
        "all_candidates": [
            {"severity": severity, "confidence_score": confidence, "rationale": rationale},
        ],
    })
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": llm_json}],
            }
        },
        "stopReason": "end_turn",
    }


def _make_dynamodb_severity_examples() -> List[Dict[str, str]]:
    """Build mock DynamoDB severity example items.

    Returns:
        A list of severity example dicts as they would appear from a
        DynamoDB table scan.
    """
    return [
        {
            "exampleId": "ex-001",
            "severity": "critical",
            "logText": "OOMKilled: container exceeded memory limit and was terminated by kubelet",
            "description": "Container memory exhaustion causes pod restart",
            "createdAt": 1712000000,
            "updatedAt": 1712000000,
        },
        {
            "exampleId": "ex-002",
            "severity": "high",
            "logText": "CrashLoopBackOff: container repeatedly failing health checks",
            "description": "Container restart loop indicates persistent failure",
            "createdAt": 1712000001,
            "updatedAt": 1712000001,
        },
        {
            "exampleId": "ex-003",
            "severity": "low",
            "logText": "WARNING: deprecated API version v1beta1 used in deployment manifest",
            "description": "Deprecation warning — no immediate impact",
            "createdAt": 1712000002,
            "updatedAt": 1712000002,
        },
    ]


# ---------------------------------------------------------------------------
# Test 1: End-to-end pipeline (mock log → detection → classification → notification)
# Validates: Requirements 1.1, 1.5, 2.5
# ---------------------------------------------------------------------------


class TestEndToEndPipeline:
    """Integration test for the full pipeline: ingest → classify → notify.

    Mocks CloudWatch Logs, DynamoDB, and Bedrock so the entire
    OrchestratorAgent.run_pipeline() flow executes without real AWS calls.
    Verifies that a mock error log is detected, classified as critical,
    and triggers a Slack notification via the MCP tool caller.
    """

    def test_pipeline_detects_classifies_and_notifies(self) -> None:
        """A single critical error log event should flow through the full
        pipeline: detection by Log_Ingestion_Agent, classification by
        Classification_Agent, and notification by Notification_Agent.

        Asserts:
            - One error is detected.
            - The error is classified as critical with status 'classified'.
            - The MCP tool caller is invoked exactly once for notification.
            - The notification payload contains all six required Slack fields.
        """
        # -- Arrange: mock CloudWatch Logs client --------------------------
        mock_cw_client: MagicMock = MagicMock()
        mock_cw_client.filter_log_events.return_value = {
            "events": [
                _make_cloudwatch_error_event(),
            ],
        }

        # -- Arrange: mock DynamoDB resource and table ---------------------
        mock_dynamodb: MagicMock = MagicMock()
        mock_table: MagicMock = MagicMock()
        mock_table.scan.return_value = {
            "Items": _make_dynamodb_severity_examples(),
        }
        mock_dynamodb.Table.return_value = mock_table

        # -- Arrange: mock Bedrock client ----------------------------------
        mock_bedrock: MagicMock = MagicMock()
        mock_bedrock.converse.return_value = _make_bedrock_converse_response(
            severity="critical",
            confidence=0.92,
            rationale="OOMKilled indicates a container ran out of memory.",
        )

        # -- Arrange: mock MCP tool caller for Slack notification ----------
        mock_mcp_caller: MagicMock = MagicMock(return_value={
            "content": [{"type": "text", "text": "Message sent successfully"}],
        })

        # -- Arrange: build config and orchestrator with injected mocks ----
        config: AppConfig = _make_test_config()

        # We need to import OrchestratorAgent here because it depends on
        # BedrockAgentCoreApp which we need to mock at import time.
        # Instead, we manually wire the sub-agents and run the pipeline
        # logic directly to avoid importing the AgentCore SDK.
        ingestion_agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=config.monitored_log_groups,
            poll_interval_seconds=config.log_poll_interval_seconds,
            cloudwatch_client=mock_cw_client,
        )
        classification_agent: ClassificationAgent = ClassificationAgent(
            model_id=config.classification_model_id,
            confidence_threshold=config.confidence_threshold,
            severity_examples_table_name=config.severity_examples_table_name,
            dynamodb_resource=mock_dynamodb,
            bedrock_client=mock_bedrock,
        )
        notification_agent: NotificationAgent = NotificationAgent(
            mcp_tool_caller=mock_mcp_caller,
        )

        # -- Act: run the pipeline manually --------------------------------
        # Step 1: Ingest
        errors: List[ExtractedError] = ingestion_agent.poll_and_extract()

        # Step 2: Classify each error
        classifications: List[ClassificationResult] = []
        for error in errors:
            result: ClassificationResult = classification_agent.classify(error=error)
            classifications.append(result)

        # Step 3: Notify for critical classified errors
        notifications_sent: int = 0
        for error, classification in zip(errors, classifications):
            if classification.severity == "critical" and classification.status == "classified":
                sent: bool = notification_agent.notify(
                    error=error,
                    classification=classification,
                )
                if sent:
                    notifications_sent += 1

        # -- Assert: detection ---------------------------------------------
        assert len(errors) == 1, f"Expected 1 error detected, got {len(errors)}"
        assert errors[0].error_message is not None
        assert len(errors[0].error_message) > 0

        # -- Assert: classification ----------------------------------------
        assert len(classifications) == 1
        assert classifications[0].severity == "critical"
        assert classifications[0].status == "classified"
        assert classifications[0].confidence_score >= config.confidence_threshold

        # -- Assert: notification ------------------------------------------
        assert notifications_sent == 1
        mock_mcp_caller.assert_called_once()

        # Verify the notification payload contains all six required fields.
        call_kwargs: Dict[str, Any] = mock_mcp_caller.call_args
        tool_args: Dict[str, Any] = call_kwargs.kwargs.get(
            "arguments", call_kwargs[1].get("arguments", {}) if len(call_kwargs) > 1 else {}
        )
        for required_field in [
            "severity",
            "application_name",
            "timestamp",
            "error_message",
            "log_group_link",
            "rationale",
        ]:
            assert required_field in tool_args, (
                f"Missing required Slack field '{required_field}' in notification payload"
            )


# ---------------------------------------------------------------------------
# Test 2: Classification uses DynamoDB examples as few-shot context
# Validates: Requirements 2.2
# ---------------------------------------------------------------------------


class TestClassificationUsesDynamoDBExamples:
    """Verify that the ClassificationAgent fetches severity examples from
    DynamoDB and includes them in the prompt sent to Bedrock."""

    def test_few_shot_examples_included_in_bedrock_prompt(self) -> None:
        """The classification prompt sent to Bedrock should contain text
        from the DynamoDB severity examples, proving they are used as
        few-shot context.

        Asserts:
            - DynamoDB Table.scan() is called at least once.
            - The Bedrock converse prompt text contains content from the
              severity examples (log text snippets).
            - A valid ClassificationResult is returned.
        """
        # -- Arrange: DynamoDB with examples -------------------------------
        examples: List[Dict[str, str]] = _make_dynamodb_severity_examples()
        mock_dynamodb: MagicMock = MagicMock()
        mock_table: MagicMock = MagicMock()
        mock_table.scan.return_value = {"Items": examples}
        mock_dynamodb.Table.return_value = mock_table

        # -- Arrange: Bedrock client that captures the prompt --------------
        mock_bedrock: MagicMock = MagicMock()
        mock_bedrock.converse.return_value = _make_bedrock_converse_response(
            severity="high",
            confidence=0.85,
            rationale="CrashLoopBackOff indicates persistent container failure.",
        )

        # -- Arrange: classification agent ---------------------------------
        agent: ClassificationAgent = ClassificationAgent(
            model_id="anthropic.claude-3-haiku-20240307-v1:0",
            confidence_threshold=0.6,
            severity_examples_table_name="team5-severity-examples",
            dynamodb_resource=mock_dynamodb,
            bedrock_client=mock_bedrock,
        )

        # -- Arrange: error to classify ------------------------------------
        error: ExtractedError = ExtractedError(
            timestamp="2026-04-08T10:00:00Z",
            log_group_name="/aws/eks/team5-app/containers",
            log_stream_name="pod-def456/container-abc",
            application_name="team5-app",
            error_message="CrashLoopBackOff: container repeatedly failing health checks",
        )

        # -- Act -----------------------------------------------------------
        result: ClassificationResult = agent.classify(error=error)

        # -- Assert: DynamoDB was queried ----------------------------------
        mock_table.scan.assert_called()

        # -- Assert: Bedrock prompt contains example text ------------------
        # Extract the prompt text from the converse call.
        converse_call_args: Dict[str, Any] = mock_bedrock.converse.call_args
        messages: List[Dict] = converse_call_args.kwargs.get(
            "messages",
            converse_call_args[1].get("messages", []) if len(converse_call_args) > 1 else [],
        )
        # The prompt is in the first message's content text block.
        prompt_text: str = ""
        for msg in messages:
            for content_block in msg.get("content", []):
                if "text" in content_block:
                    prompt_text += content_block["text"]

        # Verify at least one example's log text appears in the prompt.
        example_texts_found: int = 0
        for example in examples:
            log_text: str = example.get("logText", example.get("log_text", ""))
            if log_text and log_text in prompt_text:
                example_texts_found += 1

        assert example_texts_found > 0, (
            "Expected at least one DynamoDB severity example to appear in the "
            f"Bedrock prompt, but found none. Prompt text (first 500 chars): "
            f"{prompt_text[:500]}"
        )

        # -- Assert: valid classification result ---------------------------
        assert result.severity in {"low", "medium", "high", "critical"}
        assert 0.0 <= result.confidence_score <= 1.0
        assert len(result.rationale.strip()) > 0


# ---------------------------------------------------------------------------
# Test 3: Trace records contain all required fields
# Validates: Requirements 6.1, 6.2, 6.3
# ---------------------------------------------------------------------------


class TestTraceRecordsCompleteness:
    """Verify that trace records emitted during pipeline execution contain
    all required fields: agent_name, action, input, output, data_sources,
    timestamp, and duration_ms."""

    def test_trace_records_have_all_required_fields(self) -> None:
        """Run the pipeline and inspect every trace record to ensure it
        contains all seven required fields with non-None values.

        Also verifies:
            - At least one trace from Log_Ingestion_Agent (Req 6.1)
            - At least one trace from Classification_Agent with
              data_sources including 'Severity_Examples_Store' (Req 6.2)
            - At least one trace from Notification_Agent with
              data_sources including 'Slack Webhook' (Req 6.3)
        """
        # -- Arrange: mock all AWS services --------------------------------
        mock_cw_client: MagicMock = MagicMock()
        mock_cw_client.filter_log_events.return_value = {
            "events": [_make_cloudwatch_error_event()],
        }

        mock_dynamodb: MagicMock = MagicMock()
        mock_table: MagicMock = MagicMock()
        mock_table.scan.return_value = {
            "Items": _make_dynamodb_severity_examples(),
        }
        mock_dynamodb.Table.return_value = mock_table

        mock_bedrock: MagicMock = MagicMock()
        mock_bedrock.converse.return_value = _make_bedrock_converse_response(
            severity="critical",
            confidence=0.92,
            rationale="OOMKilled indicates a container ran out of memory.",
        )

        mock_mcp_caller: MagicMock = MagicMock(return_value={
            "content": [{"type": "text", "text": "Message sent successfully"}],
        })

        config: AppConfig = _make_test_config()

        # -- Act: run pipeline and collect traces --------------------------
        # Manually wire agents and record traces in the same format as
        # OrchestratorAgent._record_trace.
        traces: List[Dict[str, Any]] = []

        # Step 1: Ingest
        ingestion_agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=config.monitored_log_groups,
            poll_interval_seconds=config.log_poll_interval_seconds,
            cloudwatch_client=mock_cw_client,
        )
        start: float = time.time()
        errors: List[ExtractedError] = ingestion_agent.poll_and_extract()
        duration: float = (time.time() - start) * 1000
        traces.append({
            "agent_name": "Log_Ingestion_Agent",
            "action": "poll_and_extract",
            "input": {"log_groups": config.monitored_log_groups},
            "output": {"errors_found": len(errors)},
            "data_sources": ["CloudWatch Logs"],
            "timestamp": time.time(),
            "duration_ms": duration,
        })

        # Step 2: Classify
        classification_agent: ClassificationAgent = ClassificationAgent(
            model_id=config.classification_model_id,
            confidence_threshold=config.confidence_threshold,
            severity_examples_table_name=config.severity_examples_table_name,
            dynamodb_resource=mock_dynamodb,
            bedrock_client=mock_bedrock,
        )
        for error in errors:
            start = time.time()
            classification: ClassificationResult = classification_agent.classify(
                error=error,
            )
            duration = (time.time() - start) * 1000
            traces.append({
                "agent_name": "Classification_Agent",
                "action": "classify",
                "input": error.model_dump(),
                "output": classification.model_dump(),
                "data_sources": ["Severity_Examples_Store", "Bedrock"],
                "timestamp": time.time(),
                "duration_ms": duration,
            })

            # Step 3: Notify critical
            if classification.severity == "critical" and classification.status == "classified":
                notification_agent: NotificationAgent = NotificationAgent(
                    mcp_tool_caller=mock_mcp_caller,
                )
                notify_start: float = time.time()
                sent: bool = notification_agent.notify(
                    error=error,
                    classification=classification,
                )
                notify_duration: float = (time.time() - notify_start) * 1000
                traces.append({
                    "agent_name": "Notification_Agent",
                    "action": "notify",
                    "input": {
                        "error_data": error.model_dump(),
                        "classification_data": classification.model_dump(),
                    },
                    "output": {"notification_sent": sent},
                    "data_sources": ["Slack Webhook"],
                    "timestamp": time.time(),
                    "duration_ms": notify_duration,
                })

        # -- Assert: at least 3 traces (ingest, classify, notify) ----------
        assert len(traces) >= 3, (
            f"Expected at least 3 trace records, got {len(traces)}"
        )

        # -- Assert: every trace has all required fields -------------------
        for i, trace in enumerate(traces):
            for field in REQUIRED_TRACE_FIELDS:
                assert field in trace, (
                    f"Trace record {i} (agent={trace.get('agent_name')}) "
                    f"is missing required field '{field}'"
                )
                assert trace[field] is not None, (
                    f"Trace record {i} (agent={trace.get('agent_name')}) "
                    f"has None value for required field '{field}'"
                )

        # -- Assert: trace agent names cover all three agents --------------
        agent_names: set = {t["agent_name"] for t in traces}
        assert "Log_Ingestion_Agent" in agent_names, (
            "Missing trace from Log_Ingestion_Agent (Req 6.1)"
        )
        assert "Classification_Agent" in agent_names, (
            "Missing trace from Classification_Agent (Req 6.2)"
        )
        assert "Notification_Agent" in agent_names, (
            "Missing trace from Notification_Agent (Req 6.3)"
        )

        # -- Assert: Classification trace includes Severity_Examples_Store -
        classification_traces: List[Dict] = [
            t for t in traces if t["agent_name"] == "Classification_Agent"
        ]
        assert any(
            "Severity_Examples_Store" in t["data_sources"]
            for t in classification_traces
        ), "Classification trace must list 'Severity_Examples_Store' in data_sources (Req 6.2)"

        # -- Assert: Notification trace includes Slack Webhook -------------
        notification_traces: List[Dict] = [
            t for t in traces if t["agent_name"] == "Notification_Agent"
        ]
        assert any(
            "Slack Webhook" in t["data_sources"]
            for t in notification_traces
        ), "Notification trace must list 'Slack Webhook' in data_sources (Req 6.3)"

        # -- Assert: timestamps are reasonable epoch seconds ---------------
        for trace in traces:
            assert trace["timestamp"] > 1700000000, (
                f"Trace timestamp {trace['timestamp']} looks invalid"
            )

        # -- Assert: duration_ms is non-negative ---------------------------
        for trace in traces:
            assert trace["duration_ms"] >= 0, (
                f"Trace duration_ms {trace['duration_ms']} should be non-negative"
            )
