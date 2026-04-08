# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Log_Ingestion_Agent module.

Tests cover:
- Non-existent log group logs a warning and continues polling remaining groups
- Error pattern matching for ERROR, FATAL, Exception, Traceback
- Last-polled timestamp tracking prevents reprocessing

Validates: Requirements 1.1, 1.2, 1.3, 1.4
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Import modules from the pattern directory.
# ---------------------------------------------------------------------------
_PATTERN_DIR: Path = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
_AGENTS_DIR: Path = _PATTERN_DIR / "agents"

for _dir in (_PATTERN_DIR, _AGENTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

_agent_module = importlib.import_module("log_ingestion_agent")
_models_module = importlib.import_module("models")

LogIngestionAgent = _agent_module.LogIngestionAgent
extract_error = _agent_module.extract_error
extract_application_name = _agent_module.extract_application_name
ExtractedError = _models_module.ExtractedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cw_event(
    message: str,
    timestamp: int = 1_712_570_400_000,
    log_stream_name: str = "pod-abc123/container-xyz",
) -> Dict:
    """Build a mock CloudWatch log event dict.

    Args:
        message: The log message body.
        timestamp: Epoch millisecond timestamp (default: 2024-04-08T10:00:00Z).
        log_stream_name: The log stream name embedded in the event.

    Returns:
        A dict matching the CloudWatch Logs filter_log_events event shape.
    """
    return {
        "timestamp": timestamp,
        "message": message,
        "logStreamName": log_stream_name,
        "ingestionTime": timestamp + 50,
        "eventId": "evt-001",
    }


def _make_resource_not_found_error() -> ClientError:
    """Create a ClientError simulating a ResourceNotFoundException.

    Returns:
        A botocore ClientError with Code=ResourceNotFoundException.
    """
    return ClientError(
        error_response={
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "The specified log group does not exist.",
            }
        },
        operation_name="FilterLogEvents",
    )


def _make_mock_client(responses_by_group: Dict[str, List[Dict]]) -> MagicMock:
    """Create a mock CloudWatch Logs client that returns canned responses.

    The mock's ``filter_log_events`` method returns different responses
    depending on the ``logGroupName`` kwarg. If the log group name is
    mapped to a ``ClientError``, that error is raised instead.

    Args:
        responses_by_group: Mapping of log group name to either a list of
            response dicts (one per paginated call) or a single
            ClientError to raise.

    Returns:
        A MagicMock configured as a CloudWatch Logs client.
    """
    mock_client: MagicMock = MagicMock()

    # Track call index per log group for pagination simulation.
    call_indices: Dict[str, int] = {}

    def _filter_log_events(**kwargs) -> Dict:
        group: str = kwargs["logGroupName"]

        if group not in responses_by_group:
            raise _make_resource_not_found_error()

        value = responses_by_group[group]

        # If the value is a ClientError, raise it.
        if isinstance(value, ClientError):
            raise value

        idx: int = call_indices.get(group, 0)
        call_indices[group] = idx + 1

        if idx < len(value):
            return value[idx]
        # No more pages — return empty.
        return {"events": []}

    mock_client.filter_log_events = MagicMock(side_effect=_filter_log_events)
    return mock_client


# ---------------------------------------------------------------------------
# Tests: Non-existent log group handling (Requirement 1.4)
# ---------------------------------------------------------------------------


class TestNonExistentLogGroup:
    """Verify that a non-existent log group logs a warning and the agent
    continues polling the remaining groups."""

    def test_missing_group_logs_warning_and_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ResourceNotFoundException for one group should not prevent
        polling of other groups. A warning must be logged.

        Args:
            caplog: Pytest fixture for capturing log output.
        """
        existing_group: str = "/aws/eks/team5-app/containers"
        missing_group: str = "/aws/eks/team5-missing/containers"

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                existing_group: [
                    {
                        "events": [
                            _make_cw_event(
                                message="ERROR something broke",
                                timestamp=1_712_570_400_000,
                            ),
                        ],
                    }
                ],
                # missing_group is NOT in the dict, so it triggers
                # ResourceNotFoundException.
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[missing_group, existing_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        with caplog.at_level(logging.WARNING):
            errors: List[ExtractedError] = agent.poll_and_extract()

        # The agent should still return errors from the existing group.
        assert len(errors) == 1
        assert errors[0].log_group_name == existing_group

        # A warning about the missing group should be logged.
        warning_messages: List[str] = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(missing_group in msg for msg in warning_messages), (
            f"Expected a warning mentioning '{missing_group}', "
            f"got: {warning_messages}"
        )

    def test_all_groups_missing_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If all monitored groups are missing, poll_and_extract returns
        an empty list and logs warnings for each.

        Args:
            caplog: Pytest fixture for capturing log output.
        """
        groups: List[str] = [
            "/aws/eks/team5-gone1/containers",
            "/aws/eks/team5-gone2/containers",
        ]

        mock_client: MagicMock = _make_mock_client(responses_by_group={})

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=groups,
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        with caplog.at_level(logging.WARNING):
            errors: List[ExtractedError] = agent.poll_and_extract()

        assert len(errors) == 0

        # Both groups should have generated warnings.
        warning_messages: List[str] = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        for group in groups:
            assert any(group in msg for msg in warning_messages), (
                f"Expected a warning for '{group}', got: {warning_messages}"
            )


# ---------------------------------------------------------------------------
# Tests: Error pattern matching (Requirements 1.1, 1.2)
# ---------------------------------------------------------------------------


class TestErrorPatternMatching:
    """Verify that the agent detects ERROR, FATAL, Exception, and Traceback
    patterns in log messages."""

    @pytest.mark.parametrize(
        "keyword",
        ["ERROR", "FATAL", "Exception", "Traceback"],
        ids=["ERROR", "FATAL", "Exception", "Traceback"],
    )
    def test_each_error_keyword_is_detected(self, keyword: str) -> None:
        """Each of the four error keywords should be detected and produce
        an ExtractedError.

        Args:
            keyword: The error keyword to test.
        """
        log_group: str = "/aws/eks/team5-app/containers"
        message: str = f"2026-04-08T10:00:00Z {keyword} something went wrong"

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                log_group: [
                    {
                        "events": [
                            _make_cw_event(message=message),
                        ],
                    }
                ],
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[log_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        errors: List[ExtractedError] = agent.poll_and_extract()

        assert len(errors) == 1, (
            f"Expected 1 error for keyword '{keyword}', got {len(errors)}"
        )
        assert keyword in errors[0].error_message

    def test_info_messages_are_not_extracted(self) -> None:
        """Messages without error keywords should not produce ExtractedError."""
        log_group: str = "/aws/eks/team5-app/containers"

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                log_group: [
                    {
                        "events": [
                            _make_cw_event(
                                message="INFO Application started on port 8080"
                            ),
                            _make_cw_event(
                                message="WARN High memory usage detected"
                            ),
                        ],
                    }
                ],
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[log_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        errors: List[ExtractedError] = agent.poll_and_extract()
        assert len(errors) == 0

    def test_mixed_messages_only_errors_extracted(self) -> None:
        """Only messages with error keywords should be extracted from a
        mixed batch of log events."""
        log_group: str = "/aws/eks/team5-app/containers"

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                log_group: [
                    {
                        "events": [
                            _make_cw_event(message="INFO Healthy"),
                            _make_cw_event(message="ERROR Disk full"),
                            _make_cw_event(message="WARN Slow query"),
                            _make_cw_event(message="FATAL Out of memory"),
                        ],
                    }
                ],
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[log_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        errors: List[ExtractedError] = agent.poll_and_extract()
        assert len(errors) == 2
        messages: List[str] = [e.error_message for e in errors]
        assert any("ERROR" in m for m in messages)
        assert any("FATAL" in m for m in messages)

    def test_extracted_error_fields_are_populated(self) -> None:
        """All five fields of ExtractedError should be non-empty for a
        detected error event."""
        log_group: str = "/aws/eks/team5-checkout/containers"
        stream: str = "pod-abc123/container-main"

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                log_group: [
                    {
                        "events": [
                            _make_cw_event(
                                message="ERROR NullPointerException in OrderService",
                                timestamp=1_712_570_400_000,
                                log_stream_name=stream,
                            ),
                        ],
                    }
                ],
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[log_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        errors: List[ExtractedError] = agent.poll_and_extract()
        assert len(errors) == 1

        error: ExtractedError = errors[0]
        assert error.timestamp  # non-empty
        assert error.log_group_name == log_group
        assert error.log_stream_name == stream
        assert error.application_name  # non-empty, derived from log group
        assert "NullPointerException" in error.error_message


# ---------------------------------------------------------------------------
# Tests: Last-polled timestamp tracking (Requirement 1.3)
# ---------------------------------------------------------------------------


class TestTimestampTracking:
    """Verify that the agent tracks last-polled timestamps per log group
    to avoid reprocessing events."""

    def test_timestamp_advances_after_poll(self) -> None:
        """After polling, the last-polled timestamp should advance to the
        highest event timestamp seen."""
        log_group: str = "/aws/eks/team5-app/containers"
        event_ts: int = 1_712_570_400_000

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                log_group: [
                    {
                        "events": [
                            _make_cw_event(
                                message="ERROR something broke",
                                timestamp=event_ts,
                            ),
                        ],
                    }
                ],
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[log_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        # Before polling, timestamp should be 0.
        assert agent.get_last_polled_timestamp(log_group_name=log_group) == 0

        agent.poll_and_extract()

        # After polling, timestamp should match the event.
        assert agent.get_last_polled_timestamp(log_group_name=log_group) == event_ts

    def test_second_poll_uses_advanced_start_time(self) -> None:
        """The second poll should pass a startTime greater than the first
        event's timestamp, preventing reprocessing."""
        log_group: str = "/aws/eks/team5-app/containers"
        first_event_ts: int = 1_712_570_400_000
        second_event_ts: int = 1_712_570_500_000

        call_count: int = 0

        def _filter_side_effect(**kwargs) -> Dict:
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # First poll: return the first event.
                return {
                    "events": [
                        _make_cw_event(
                            message="ERROR first error",
                            timestamp=first_event_ts,
                        ),
                    ],
                }
            elif call_count == 2:
                # Second poll: verify startTime is advanced, return new event.
                start_time: int = kwargs.get("startTime", 0)
                assert start_time > first_event_ts, (
                    f"Expected startTime > {first_event_ts}, got {start_time}. "
                    f"The agent should advance the start time to avoid reprocessing."
                )
                return {
                    "events": [
                        _make_cw_event(
                            message="ERROR second error",
                            timestamp=second_event_ts,
                        ),
                    ],
                }
            else:
                return {"events": []}

        mock_client: MagicMock = MagicMock()
        mock_client.filter_log_events = MagicMock(side_effect=_filter_side_effect)

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[log_group],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        # First poll.
        first_errors: List[ExtractedError] = agent.poll_and_extract()
        assert len(first_errors) == 1

        # Second poll — the side_effect asserts startTime is advanced.
        second_errors: List[ExtractedError] = agent.poll_and_extract()
        assert len(second_errors) == 1
        assert "second error" in second_errors[0].error_message

    def test_timestamps_tracked_independently_per_group(self) -> None:
        """Each log group should have its own independent last-polled
        timestamp."""
        group_a: str = "/aws/eks/team5-app-a/containers"
        group_b: str = "/aws/eks/team5-app-b/containers"
        ts_a: int = 1_712_570_400_000
        ts_b: int = 1_712_570_600_000

        mock_client: MagicMock = _make_mock_client(
            responses_by_group={
                group_a: [
                    {
                        "events": [
                            _make_cw_event(
                                message="ERROR error in app-a",
                                timestamp=ts_a,
                            ),
                        ],
                    }
                ],
                group_b: [
                    {
                        "events": [
                            _make_cw_event(
                                message="FATAL crash in app-b",
                                timestamp=ts_b,
                            ),
                        ],
                    }
                ],
            }
        )

        agent: LogIngestionAgent = LogIngestionAgent(
            monitored_log_groups=[group_a, group_b],
            poll_interval_seconds=30,
            cloudwatch_client=mock_client,
        )

        agent.poll_and_extract()

        assert agent.get_last_polled_timestamp(log_group_name=group_a) == ts_a
        assert agent.get_last_polled_timestamp(log_group_name=group_b) == ts_b


# ---------------------------------------------------------------------------
# Tests: extract_application_name helper
# ---------------------------------------------------------------------------


class TestExtractApplicationName:
    """Verify the application name extraction heuristic."""

    def test_eks_log_group_pattern(self) -> None:
        """Standard EKS log group pattern extracts the app name segment."""
        result: str = extract_application_name(
            log_group_name="/aws/eks/team5-checkout/containers",
            log_stream_name="pod-abc/container-main",
        )
        assert result == "team5-checkout"

    def test_fallback_to_last_segment(self) -> None:
        """Non-standard log group falls back to the last path segment."""
        result: str = extract_application_name(
            log_group_name="/custom/my-service",
            log_stream_name="stream-1",
        )
        assert result == "my-service"

    def test_fallback_to_stream_name(self) -> None:
        """Empty log group name falls back to the log stream name."""
        result: str = extract_application_name(
            log_group_name="",
            log_stream_name="pod-xyz/container-main",
        )
        assert result == "pod-xyz"

    def test_both_empty_returns_unknown(self) -> None:
        """Both empty strings should return 'unknown'."""
        result: str = extract_application_name(
            log_group_name="",
            log_stream_name="",
        )
        assert result == "unknown"
