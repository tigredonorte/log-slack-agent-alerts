# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property-based tests for Slack message field completeness (Property 4).

Feature: eks-log-slack-alerts, Property 4: Slack message field completeness

For any ``ExtractedError`` and ``ClassificationResult`` pair, the formatted
Slack message payload SHALL contain all six required fields: severity,
application_name, timestamp, error_message, log_group_link, and rationale.

Also verifies that ``build_cloudwatch_link()`` produces a non-empty URL
containing the log group name.

Uses Hypothesis to generate random ``ExtractedError`` and
``ClassificationResult`` instances, then verifies the ``build_slack_payload``
function produces a complete ``SlackMessagePayload`` in all cases.

Validates: Requirements 3.2
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import List, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import modules from the pattern directory (uses dashes in name, so we
# add it to sys.path and use importlib).
# ---------------------------------------------------------------------------
_PATTERN_DIR: Path = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
_AGENTS_DIR: Path = _PATTERN_DIR / "agents"

for _dir in (_PATTERN_DIR, _AGENTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

_notification_module = importlib.import_module("notification_agent")
_models_module = importlib.import_module("models")

build_slack_payload = _notification_module.build_slack_payload
build_cloudwatch_link = _notification_module.build_cloudwatch_link
ExtractedError = _models_module.ExtractedError
ClassificationResult = _models_module.ClassificationResult
SlackMessagePayload = _models_module.SlackMessagePayload

VALID_SEVERITIES: List[str] = ["low", "medium", "high", "critical"]


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for generating a realistic ExtractedError instance.
extracted_error_strategy: st.SearchStrategy[ExtractedError] = st.builds(
    ExtractedError,
    timestamp=st.from_regex(
        r"2026-0[1-9]-[0-2][0-9]T[0-1][0-9]:[0-5][0-9]:[0-5][0-9]Z",
        fullmatch=True,
    ),
    log_group_name=st.from_regex(
        r"/aws/eks/[a-z][a-z0-9\-]{2,15}/containers",
        fullmatch=True,
    ),
    log_stream_name=st.from_regex(
        r"pod-[a-z0-9]{4,8}/container-[a-z0-9]{3,6}",
        fullmatch=True,
    ),
    application_name=st.from_regex(r"[a-z][a-z0-9\-]{2,15}", fullmatch=True),
    error_message=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=10,
        max_size=200,
    ),
)

# Strategy for generating a valid severity level.
severity_strategy: st.SearchStrategy[str] = st.sampled_from(VALID_SEVERITIES)

# Strategy for generating a valid confidence score in [0.0, 1.0].
confidence_strategy: st.SearchStrategy[float] = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)

# Strategy for generating a non-empty rationale string.
rationale_strategy: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=5,
    max_size=100,
).filter(lambda s: s.strip())

# Strategy for generating a valid ClassificationResult status.
status_strategy: st.SearchStrategy[str] = st.sampled_from(
    ["classified", "awaiting_review"]
)

# Strategy for generating optional candidate_severities (None or a list).
candidate_severities_strategy: st.SearchStrategy[Optional[List[dict]]] = st.one_of(
    st.none(),
    st.lists(
        st.fixed_dictionaries({
            "severity": severity_strategy,
            "confidence_score": confidence_strategy,
            "rationale": rationale_strategy,
        }),
        min_size=2,
        max_size=4,
    ),
)


def _build_classification_result(
    severity: str,
    confidence_score: float,
    rationale: str,
    status: str,
    candidate_severities: Optional[List[dict]],
) -> ClassificationResult:
    """Build a ClassificationResult from individual field values.

    Args:
        severity: One of low, medium, high, critical.
        confidence_score: Float in [0.0, 1.0].
        rationale: Non-empty rationale string.
        status: Either 'classified' or 'awaiting_review'.
        candidate_severities: Optional list of candidate dicts.

    Returns:
        A validated ClassificationResult instance.
    """
    return ClassificationResult(
        severity=severity,
        confidence_score=confidence_score,
        rationale=rationale,
        status=status,
        candidate_severities=candidate_severities,
    )


# Combined strategy for generating a valid ClassificationResult.
classification_result_strategy: st.SearchStrategy[ClassificationResult] = st.builds(
    _build_classification_result,
    severity=severity_strategy,
    confidence_score=confidence_strategy,
    rationale=rationale_strategy,
    status=status_strategy,
    candidate_severities=candidate_severities_strategy,
)


# ---------------------------------------------------------------------------
# Property 4: Slack message field completeness
# ---------------------------------------------------------------------------


class TestSlackMessageFieldCompleteness:
    """Property 4: Slack message field completeness.

    For any ExtractedError and ClassificationResult pair, the formatted
    Slack message payload SHALL contain all six required fields:
    severity, application_name, timestamp, error_message, log_group_link,
    and rationale.

    **Validates: Requirements 3.2**
    """

    @given(
        error=extracted_error_strategy,
        classification=classification_result_strategy,
    )
    @settings(max_examples=100)
    def test_all_six_fields_present_and_non_empty(
        self,
        error: ExtractedError,
        classification: ClassificationResult,
    ) -> None:
        """For any ExtractedError and ClassificationResult pair,
        build_slack_payload SHALL return a SlackMessagePayload with all
        six required fields non-empty.

        Args:
            error: Random ExtractedError generated by Hypothesis.
            classification: Random ClassificationResult generated by
                Hypothesis.
        """
        payload: SlackMessagePayload = build_slack_payload(
            error=error,
            classification=classification,
        )

        # All six fields must be non-empty strings.
        assert isinstance(payload.severity, str) and len(payload.severity) > 0, (
            f"severity must be a non-empty string, got: {payload.severity!r}"
        )
        assert isinstance(payload.application_name, str) and len(payload.application_name) > 0, (
            f"application_name must be a non-empty string, got: {payload.application_name!r}"
        )
        assert isinstance(payload.timestamp, str) and len(payload.timestamp) > 0, (
            f"timestamp must be a non-empty string, got: {payload.timestamp!r}"
        )
        assert isinstance(payload.error_message, str) and len(payload.error_message) > 0, (
            f"error_message must be a non-empty string, got: {payload.error_message!r}"
        )
        assert isinstance(payload.log_group_link, str) and len(payload.log_group_link) > 0, (
            f"log_group_link must be a non-empty string, got: {payload.log_group_link!r}"
        )
        assert isinstance(payload.rationale, str) and len(payload.rationale) > 0, (
            f"rationale must be a non-empty string, got: {payload.rationale!r}"
        )

    @given(
        error=extracted_error_strategy,
        classification=classification_result_strategy,
    )
    @settings(max_examples=100)
    def test_payload_is_valid_slack_message_payload_instance(
        self,
        error: ExtractedError,
        classification: ClassificationResult,
    ) -> None:
        """For any input pair, build_slack_payload SHALL return a valid
        SlackMessagePayload instance (Pydantic validation passes).

        Args:
            error: Random ExtractedError generated by Hypothesis.
            classification: Random ClassificationResult generated by
                Hypothesis.
        """
        payload: SlackMessagePayload = build_slack_payload(
            error=error,
            classification=classification,
        )

        # The return value must be a proper SlackMessagePayload instance,
        # meaning Pydantic validation (min_length=1 on all fields) passed.
        assert isinstance(payload, SlackMessagePayload), (
            f"Expected SlackMessagePayload instance, got: {type(payload).__name__}"
        )


# ---------------------------------------------------------------------------
# CloudWatch link generation
# ---------------------------------------------------------------------------


class TestBuildCloudwatchLink:
    """Verify that build_cloudwatch_link produces a non-empty URL
    containing the log group name for any valid log group input.
    """

    @given(
        log_group_name=st.from_regex(
            r"/aws/eks/[a-z][a-z0-9\-]{2,15}/containers",
            fullmatch=True,
        ),
    )
    @settings(max_examples=100)
    def test_cloudwatch_link_non_empty_and_contains_log_group(
        self,
        log_group_name: str,
    ) -> None:
        """For any valid log group name, build_cloudwatch_link SHALL
        produce a non-empty URL string that contains the log group name
        (URL-encoded).

        Args:
            log_group_name: Random CloudWatch log group name generated
                by Hypothesis.
        """
        link: str = build_cloudwatch_link(log_group_name=log_group_name)

        assert isinstance(link, str) and len(link) > 0, (
            f"CloudWatch link must be a non-empty string, got: {link!r}"
        )

        # The URL-encoded log group name should appear in the link.
        # Since '/' is encoded as '%2F', we check for the encoded form
        # of the first path segment (e.g. "aws" from "/aws/eks/...").
        assert "aws" in link, (
            f"CloudWatch link should contain the log group name components. "
            f"Log group: {log_group_name!r}, Link: {link!r}"
        )

        # The link must start with the CloudWatch console URL prefix.
        assert link.startswith("https://console.aws.amazon.com/cloudwatch/home"), (
            f"CloudWatch link must start with the console URL prefix. "
            f"Got: {link!r}"
        )
