# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the EKS Log Alerts shared data models.

Tests validation logic for ExtractedError, ClassificationResult,
SlackMessagePayload, and SeverityExample Pydantic models.
"""

import importlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# The pattern directory uses dashes ("eks-log-alerts") which is not a valid
# Python package name. We add the pattern directory to sys.path and import
# the models module directly.
_PATTERN_DIR = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
sys.path.insert(0, str(_PATTERN_DIR))
models = importlib.import_module("models")

ExtractedError = models.ExtractedError
ClassificationResult = models.ClassificationResult
SlackMessagePayload = models.SlackMessagePayload
SeverityExample = models.SeverityExample


# ---------------------------------------------------------------------------
# ExtractedError tests
# ---------------------------------------------------------------------------


class TestExtractedError:
    """Tests for the ExtractedError model."""

    def test_valid_extracted_error(self) -> None:
        """Verify that a fully populated ExtractedError is accepted."""
        error = ExtractedError(
            timestamp="2024-01-15T10:30:00Z",
            log_group_name="/aws/eks/team5-app/containers",
            log_stream_name="pod-abc123/container-xyz",
            application_name="team5-app",
            error_message="NullPointerException in UserService.getUser()",
        )
        assert error.timestamp == "2024-01-15T10:30:00Z"
        assert error.log_group_name == "/aws/eks/team5-app/containers"
        assert error.log_stream_name == "pod-abc123/container-xyz"
        assert error.application_name == "team5-app"
        assert error.error_message == "NullPointerException in UserService.getUser()"

    def test_empty_timestamp_rejected(self) -> None:
        """Verify that an empty timestamp is rejected."""
        with pytest.raises(ValidationError, match="timestamp"):
            ExtractedError(
                timestamp="",
                log_group_name="/aws/eks/team5-app/containers",
                log_stream_name="pod-abc123/container-xyz",
                application_name="team5-app",
                error_message="Some error",
            )

    def test_missing_field_rejected(self) -> None:
        """Verify that omitting a required field raises ValidationError."""
        with pytest.raises(ValidationError):
            ExtractedError(
                timestamp="2024-01-15T10:30:00Z",
                log_group_name="/aws/eks/team5-app/containers",
                # log_stream_name intentionally omitted
                application_name="team5-app",
                error_message="Some error",
            )


# ---------------------------------------------------------------------------
# ClassificationResult tests
# ---------------------------------------------------------------------------


class TestClassificationResult:
    """Tests for the ClassificationResult model."""

    def test_valid_classified_result(self) -> None:
        """Verify that a valid classified result is accepted."""
        result = ClassificationResult(
            severity="critical",
            confidence_score=0.95,
            rationale="OOMKilled indicates the container ran out of memory.",
            candidate_severities=None,
            status="classified",
        )
        assert result.severity == "critical"
        assert result.confidence_score == 0.95
        assert result.status == "classified"

    def test_valid_awaiting_review_result(self) -> None:
        """Verify that an awaiting_review result with candidates is accepted."""
        result = ClassificationResult(
            severity="high",
            confidence_score=0.45,
            rationale="Could be high or critical based on context.",
            candidate_severities=[
                {"severity": "high", "confidence_score": 0.45, "rationale": "Reason A"},
                {"severity": "critical", "confidence_score": 0.40, "rationale": "Reason B"},
            ],
            status="awaiting_review",
        )
        assert result.status == "awaiting_review"
        assert len(result.candidate_severities) == 2

    def test_invalid_severity_rejected(self) -> None:
        """Verify that a severity not in {low, medium, high, critical} is rejected."""
        with pytest.raises(ValidationError, match="severity"):
            ClassificationResult(
                severity="urgent",
                confidence_score=0.8,
                rationale="Some rationale.",
                status="classified",
            )

    def test_confidence_score_below_zero_rejected(self) -> None:
        """Verify that a confidence_score below 0.0 is rejected."""
        with pytest.raises(ValidationError, match="confidence_score"):
            ClassificationResult(
                severity="low",
                confidence_score=-0.1,
                rationale="Some rationale.",
                status="classified",
            )

    def test_confidence_score_above_one_rejected(self) -> None:
        """Verify that a confidence_score above 1.0 is rejected."""
        with pytest.raises(ValidationError, match="confidence_score"):
            ClassificationResult(
                severity="low",
                confidence_score=1.1,
                rationale="Some rationale.",
                status="classified",
            )

    def test_confidence_score_boundary_zero_accepted(self) -> None:
        """Verify that confidence_score=0.0 is accepted (lower boundary)."""
        result = ClassificationResult(
            severity="low",
            confidence_score=0.0,
            rationale="Very uncertain classification.",
            status="awaiting_review",
        )
        assert result.confidence_score == 0.0

    def test_confidence_score_boundary_one_accepted(self) -> None:
        """Verify that confidence_score=1.0 is accepted (upper boundary)."""
        result = ClassificationResult(
            severity="critical",
            confidence_score=1.0,
            rationale="Absolutely certain this is critical.",
            status="classified",
        )
        assert result.confidence_score == 1.0

    def test_empty_rationale_rejected(self) -> None:
        """Verify that an empty rationale string is rejected."""
        with pytest.raises(ValidationError, match="rationale"):
            ClassificationResult(
                severity="medium",
                confidence_score=0.7,
                rationale="",
                status="classified",
            )

    def test_whitespace_only_rationale_rejected(self) -> None:
        """Verify that a whitespace-only rationale is rejected."""
        with pytest.raises(ValidationError, match="rationale"):
            ClassificationResult(
                severity="medium",
                confidence_score=0.7,
                rationale="   ",
                status="classified",
            )

    def test_invalid_status_rejected(self) -> None:
        """Verify that a status not in {classified, awaiting_review} is rejected."""
        with pytest.raises(ValidationError, match="status"):
            ClassificationResult(
                severity="low",
                confidence_score=0.8,
                rationale="Some rationale.",
                status="pending",
            )


# ---------------------------------------------------------------------------
# SlackMessagePayload tests
# ---------------------------------------------------------------------------


class TestSlackMessagePayload:
    """Tests for the SlackMessagePayload model."""

    def test_valid_slack_payload(self) -> None:
        """Verify that a fully populated SlackMessagePayload is accepted."""
        payload = SlackMessagePayload(
            severity="critical",
            application_name="team5-app",
            timestamp="2024-01-15T10:30:00Z",
            error_message="OOMKilled in pod team5-app-abc123",
            log_group_link="https://console.aws.amazon.com/cloudwatch/home",
            rationale="Container was killed due to memory exhaustion.",
        )
        assert payload.severity == "critical"
        assert payload.application_name == "team5-app"

    def test_empty_field_rejected(self) -> None:
        """Verify that an empty required field is rejected."""
        with pytest.raises(ValidationError, match="error_message"):
            SlackMessagePayload(
                severity="critical",
                application_name="team5-app",
                timestamp="2024-01-15T10:30:00Z",
                error_message="",
                log_group_link="https://example.com",
                rationale="Some rationale.",
            )


# ---------------------------------------------------------------------------
# SeverityExample tests
# ---------------------------------------------------------------------------


class TestSeverityExample:
    """Tests for the SeverityExample model."""

    def test_valid_severity_example(self) -> None:
        """Verify that a valid SeverityExample is accepted."""
        example = SeverityExample(
            example_id="550e8400-e29b-41d4-a716-446655440000",
            severity="high",
            log_text="ERROR: Connection refused to database at 10.0.1.5:5432",
            description="Database connectivity failure",
            created_at=1705312200000,
            updated_at=1705312200000,
        )
        assert example.severity == "high"
        assert example.description == "Database connectivity failure"

    def test_valid_example_without_description(self) -> None:
        """Verify that description is optional and defaults to None."""
        example = SeverityExample(
            example_id="550e8400-e29b-41d4-a716-446655440000",
            severity="low",
            log_text="WARN: Deprecated API call detected in request handler",
            created_at=1705312200000,
            updated_at=1705312200000,
        )
        assert example.description is None

    def test_log_text_too_short_rejected(self) -> None:
        """Verify that log_text shorter than 10 characters is rejected (Req 4.5)."""
        with pytest.raises(ValidationError, match="log_text"):
            SeverityExample(
                example_id="550e8400-e29b-41d4-a716-446655440000",
                severity="low",
                log_text="short",
                created_at=1705312200000,
                updated_at=1705312200000,
            )

    def test_log_text_exactly_10_chars_accepted(self) -> None:
        """Verify that log_text with exactly 10 characters is accepted."""
        example = SeverityExample(
            example_id="550e8400-e29b-41d4-a716-446655440000",
            severity="medium",
            log_text="1234567890",
            created_at=1705312200000,
            updated_at=1705312200000,
        )
        assert len(example.log_text) == 10

    def test_invalid_severity_rejected(self) -> None:
        """Verify that an invalid severity level is rejected (Req 4.5)."""
        with pytest.raises(ValidationError, match="severity"):
            SeverityExample(
                example_id="550e8400-e29b-41d4-a716-446655440000",
                severity="warning",
                log_text="ERROR: Something went wrong in the application",
                created_at=1705312200000,
                updated_at=1705312200000,
            )

    def test_all_valid_severity_levels_accepted(self) -> None:
        """Verify that all four valid severity levels are accepted."""
        for severity in ("low", "medium", "high", "critical"):
            example = SeverityExample(
                example_id="550e8400-e29b-41d4-a716-446655440000",
                severity=severity,
                log_text="ERROR: Test log entry for severity validation",
                created_at=1705312200000,
                updated_at=1705312200000,
            )
            assert example.severity == severity
