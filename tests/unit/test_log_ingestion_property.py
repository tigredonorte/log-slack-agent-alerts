# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property-based test for log event field extraction completeness (Property 1).

Feature: eks-log-slack-alerts, Property 1: Log event field extraction completeness

For any CloudWatch log event containing an error pattern, the
``extract_error`` function SHALL produce an ``ExtractedError`` with all
five fields non-empty: timestamp, log_group_name, log_stream_name,
application_name, and error_message.

Uses Hypothesis to generate random CloudWatch log event dicts that contain
at least one error pattern keyword, and verifies that every extracted
``ExtractedError`` has all five fields populated with non-empty strings.

Validates: Requirements 1.2
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Dict, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import modules from the pattern directory (uses dashes in name, so we
# add it to sys.path and use importlib).
# ---------------------------------------------------------------------------
_PATTERN_DIR: Path = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
_AGENTS_DIR: Path = _PATTERN_DIR / "agents"

# Ensure both the pattern dir (for models) and agents dir are importable.
for _dir in (_PATTERN_DIR, _AGENTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

_agent_module = importlib.import_module("log_ingestion_agent")
_models_module = importlib.import_module("models")

extract_error = _agent_module.extract_error
ExtractedError = _models_module.ExtractedError


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating realistic CloudWatch log events
# ---------------------------------------------------------------------------

# Error pattern keywords that the agent is expected to detect.
ERROR_KEYWORDS = ["ERROR", "FATAL", "Exception", "Traceback"]

# Strategy for generating a realistic epoch-millisecond timestamp.
# Range: 2020-01-01 to 2030-01-01 in epoch ms.
epoch_ms_strategy: st.SearchStrategy[int] = st.integers(
    min_value=1_577_836_800_000,  # 2020-01-01T00:00:00Z
    max_value=1_893_456_000_000,  # 2030-01-01T00:00:00Z
)

# Strategy for generating a non-empty log group name with realistic
# CloudWatch path structure.
log_group_name_strategy: st.SearchStrategy[str] = st.from_regex(
    r"/aws/eks/[a-z][a-z0-9\-]{2,20}/containers",
    fullmatch=True,
)

# Strategy for generating a non-empty log stream name with realistic
# pod/container structure.
log_stream_name_strategy: st.SearchStrategy[str] = st.from_regex(
    r"pod-[a-z0-9]{6,12}/container-[a-z0-9]{3,8}",
    fullmatch=True,
)

# Strategy for generating a message body that contains at least one
# error keyword. We sandwich the keyword between random text to ensure
# the regex matching works regardless of position.
error_message_strategy: st.SearchStrategy[str] = st.builds(
    lambda prefix, keyword, suffix: f"{prefix} {keyword} {suffix}",
    prefix=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=1,
        max_size=50,
    ),
    keyword=st.sampled_from(ERROR_KEYWORDS),
    suffix=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=1,
        max_size=100,
    ),
)


def _build_cloudwatch_event(
    timestamp: int,
    message: str,
    log_stream_name: str,
) -> Dict:
    """Build a CloudWatch log event dict matching the filter_log_events API shape.

    Args:
        timestamp: Epoch millisecond timestamp for the event.
        message: The log message body.
        log_stream_name: The log stream name to embed in the event.

    Returns:
        A dict with keys matching the CloudWatch Logs API event format.
    """
    return {
        "timestamp": timestamp,
        "message": message,
        "logStreamName": log_stream_name,
        "ingestionTime": timestamp + 100,
        "eventId": "test-event-id",
    }


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestLogEventFieldExtractionProperty:
    """Property 1: Log event field extraction completeness.

    For any CloudWatch log event containing an error pattern,
    ``extract_error`` SHALL produce an ``ExtractedError`` with all five
    fields non-empty.
    """

    @given(
        timestamp=epoch_ms_strategy,
        log_group_name=log_group_name_strategy,
        log_stream_name=log_stream_name_strategy,
        message=error_message_strategy,
    )
    @settings(max_examples=100)
    def test_all_fields_non_empty_for_error_events(
        self,
        timestamp: int,
        log_group_name: str,
        log_stream_name: str,
        message: str,
    ) -> None:
        """For any CloudWatch event with an error keyword, extract_error
        returns an ExtractedError where every field is a non-empty string.

        This is the core Property 1 assertion: field extraction is
        complete for all valid error events.

        Args:
            timestamp: Random epoch-ms timestamp generated by Hypothesis.
            log_group_name: Random CloudWatch log group name.
            log_stream_name: Random CloudWatch log stream name.
            message: Random log message containing at least one error keyword.
        """
        event: Dict = _build_cloudwatch_event(
            timestamp=timestamp,
            message=message,
            log_stream_name=log_stream_name,
        )

        result: Optional[ExtractedError] = extract_error(
            event=event,
            log_group_name=log_group_name,
            log_stream_name=log_stream_name,
        )

        # The event contains an error keyword, so extract_error must
        # return a non-None result.
        assert result is not None, (
            f"extract_error returned None for an event containing an error "
            f"keyword. Message: {message!r}"
        )

        # All five fields must be non-empty strings.
        assert isinstance(result.timestamp, str) and len(result.timestamp) > 0, (
            f"timestamp is empty. Got: {result.timestamp!r}"
        )
        assert isinstance(result.log_group_name, str) and len(result.log_group_name) > 0, (
            f"log_group_name is empty. Got: {result.log_group_name!r}"
        )
        assert isinstance(result.log_stream_name, str) and len(result.log_stream_name) > 0, (
            f"log_stream_name is empty. Got: {result.log_stream_name!r}"
        )
        assert isinstance(result.application_name, str) and len(result.application_name) > 0, (
            f"application_name is empty. Got: {result.application_name!r}"
        )
        assert isinstance(result.error_message, str) and len(result.error_message) > 0, (
            f"error_message is empty. Got: {result.error_message!r}"
        )

    @given(
        timestamp=epoch_ms_strategy,
        log_group_name=log_group_name_strategy,
        log_stream_name=log_stream_name_strategy,
        keyword=st.sampled_from(ERROR_KEYWORDS),
    )
    @settings(max_examples=100)
    def test_each_error_keyword_individually_detected(
        self,
        timestamp: int,
        log_group_name: str,
        log_stream_name: str,
        keyword: str,
    ) -> None:
        """Each individual error keyword (ERROR, FATAL, Exception,
        Traceback) is detected and produces a complete ExtractedError.

        This ensures the regex pattern handles all four keywords
        regardless of surrounding context.

        Args:
            timestamp: Random epoch-ms timestamp generated by Hypothesis.
            log_group_name: Random CloudWatch log group name.
            log_stream_name: Random CloudWatch log stream name.
            keyword: One of the four error keywords.
        """
        message: str = f"2026-04-08T10:00:00Z {keyword} something went wrong in pod-xyz"
        event: Dict = _build_cloudwatch_event(
            timestamp=timestamp,
            message=message,
            log_stream_name=log_stream_name,
        )

        result: Optional[ExtractedError] = extract_error(
            event=event,
            log_group_name=log_group_name,
            log_stream_name=log_stream_name,
        )

        assert result is not None, (
            f"extract_error returned None for keyword '{keyword}' in message: {message!r}"
        )

        # Verify all fields are populated.
        assert result.timestamp and result.log_group_name and result.log_stream_name
        assert result.application_name and result.error_message

    @given(
        timestamp=epoch_ms_strategy,
        log_group_name=log_group_name_strategy,
        log_stream_name=log_stream_name_strategy,
    )
    @settings(max_examples=100)
    def test_non_error_events_return_none(
        self,
        timestamp: int,
        log_group_name: str,
        log_stream_name: str,
    ) -> None:
        """Events that do NOT contain any error keyword should return None.

        This is the inverse property: extract_error only produces output
        for genuine error events.

        Args:
            timestamp: Random epoch-ms timestamp generated by Hypothesis.
            log_group_name: Random CloudWatch log group name.
            log_stream_name: Random CloudWatch log stream name.
        """
        # A message with no error keywords at all.
        message: str = "2026-04-08T10:00:00Z INFO Application started successfully on port 8080"
        event: Dict = _build_cloudwatch_event(
            timestamp=timestamp,
            message=message,
            log_stream_name=log_stream_name,
        )

        result: Optional[ExtractedError] = extract_error(
            event=event,
            log_group_name=log_group_name,
            log_stream_name=log_stream_name,
        )

        assert result is None, (
            f"extract_error should return None for non-error message, "
            f"but got: {result}"
        )
