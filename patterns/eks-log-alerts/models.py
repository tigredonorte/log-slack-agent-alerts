# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared data models for the EKS Log Alerts pattern.

Defines Pydantic models used across the multi-agent pipeline:
- ExtractedError: structured error data extracted from CloudWatch log events
- ClassificationResult: severity classification output from the Classification_Agent
- SlackMessagePayload: formatted payload sent to the Slack incoming webhook
- SeverityExample: admin-curated example stored in DynamoDB for few-shot classification

All models enforce strict validation:
- severity must be one of: low, medium, high, critical
- confidence_score must be a float in [0.0, 1.0]
- log_text must be at least 10 characters

Validates: Requirements 2.1, 2.3, 2.6, 4.5
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Allowed severity levels — used as a Literal type across all models
# ---------------------------------------------------------------------------
SeverityLevel = Literal["low", "medium", "high", "critical"]

# Minimum length for log text in severity examples (Requirement 4.5)
MIN_LOG_TEXT_LENGTH: int = 10


class ExtractedError(BaseModel):
    """Structured error data extracted from a CloudWatch log event.

    Produced by the Log_Ingestion_Agent after polling CloudWatch log groups
    and matching error patterns (ERROR, FATAL, Exception, Traceback).

    Attributes:
        timestamp: ISO 8601 timestamp from the CloudWatch log event.
        log_group_name: Full CloudWatch log group name
            (e.g. "/aws/eks/team5-app/containers").
        log_stream_name: CloudWatch log stream name
            (e.g. "pod-abc123/container-xyz").
        application_name: Application identifier derived from the log group
            or stream metadata.
        error_message: Full error text extracted from the log event.
    """

    timestamp: str = Field(
        ...,
        min_length=1,
        description="ISO 8601 timestamp from the CloudWatch log event",
    )
    log_group_name: str = Field(
        ...,
        min_length=1,
        description="Full CloudWatch log group name",
    )
    log_stream_name: str = Field(
        ...,
        min_length=1,
        description="CloudWatch log stream name",
    )
    application_name: str = Field(
        ...,
        min_length=1,
        description="Application identifier derived from log group or stream metadata",
    )
    error_message: str = Field(
        ...,
        min_length=1,
        description="Full error text extracted from the log event",
    )


class ClassificationResult(BaseModel):
    """Severity classification output from the Classification_Agent.

    Contains the assigned severity level, a confidence score, a human-readable
    rationale, and — when confidence is low or ambiguous — the top candidate
    severity levels for human review.

    Attributes:
        severity: Exactly one of low, medium, high, or critical.
        confidence_score: Float in [0.0, 1.0] representing classification
            certainty.
        rationale: One-to-two sentence explanation of why the severity was
            chosen.
        candidate_severities: Optional list of top candidate severity dicts
            (each with severity, confidence_score, rationale) populated when
            confidence is below threshold or classification is ambiguous.
        status: Either "classified" (auto-accepted) or "awaiting_review"
            (held for human escalation).
    """

    severity: SeverityLevel = Field(
        ...,
        description="Assigned severity level: low, medium, high, or critical",
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Classification certainty score in [0.0, 1.0]",
    )
    rationale: str = Field(
        ...,
        min_length=1,
        description="Brief rationale (1-2 sentences) explaining the severity assignment",
    )
    candidate_severities: Optional[List[dict]] = Field(
        default=None,
        description=(
            "Top candidate severity levels with their confidence scores and "
            "rationales. Populated when confidence is below threshold or "
            "classification is ambiguous."
        ),
    )
    status: Literal["classified", "awaiting_review"] = Field(
        ...,
        description=(
            'Either "classified" (auto-accepted) or "awaiting_review" '
            "(held for human escalation)"
        ),
    )

    @field_validator("rationale")
    @classmethod
    def rationale_must_not_be_blank(cls, v: str) -> str:
        """Validate that rationale is not just whitespace.

        Args:
            v: The rationale string to validate.

        Returns:
            The stripped rationale string.

        Raises:
            ValueError: If the rationale is empty or whitespace-only.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("rationale must contain non-whitespace characters")
        return stripped


class SlackMessagePayload(BaseModel):
    """Formatted payload sent to the Slack incoming webhook.

    Contains all six fields required by the Notification_Agent when posting
    a critical-error alert to Slack (Requirement 3.2).

    Attributes:
        severity: Error severity level string.
        application_name: Name of the application that produced the error.
        timestamp: ISO 8601 timestamp of the error event.
        error_message: Full error log text.
        log_group_link: URL deep-link to the CloudWatch log group in the
            AWS console.
        rationale: Classification rationale explaining why this severity
            was assigned.
    """

    severity: str = Field(
        ...,
        min_length=1,
        description="Error severity level",
    )
    application_name: str = Field(
        ...,
        min_length=1,
        description="Name of the application that produced the error",
    )
    timestamp: str = Field(
        ...,
        min_length=1,
        description="ISO 8601 timestamp of the error event",
    )
    error_message: str = Field(
        ...,
        min_length=1,
        description="Full error log text",
    )
    log_group_link: str = Field(
        ...,
        min_length=1,
        description="URL deep-link to the CloudWatch log group in the AWS console",
    )
    rationale: str = Field(
        ...,
        min_length=1,
        description="Classification rationale explaining the severity assignment",
    )


class SeverityExample(BaseModel):
    """Admin-curated severity example stored in DynamoDB.

    Used as few-shot examples by the Classification_Agent to improve
    severity classification accuracy. Managed via the Admin_API CRUD
    endpoints.

    Attributes:
        example_id: UUID identifier for the example (partition key in
            DynamoDB).
        severity: One of low, medium, high, or critical.
        log_text: Sample log text demonstrating this severity level.
            Must be at least 10 characters (Requirement 4.5).
        description: Optional human-written description explaining why
            this log text maps to the given severity.
        created_at: Epoch milliseconds when the example was created.
        updated_at: Epoch milliseconds when the example was last updated.
    """

    example_id: str = Field(
        ...,
        min_length=1,
        description="UUID identifier for the example (DynamoDB partition key)",
    )
    severity: SeverityLevel = Field(
        ...,
        description="Severity level: low, medium, high, or critical",
    )
    log_text: str = Field(
        ...,
        min_length=MIN_LOG_TEXT_LENGTH,
        description=(
            f"Sample log text (minimum {MIN_LOG_TEXT_LENGTH} characters) "
            "demonstrating this severity level"
        ),
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional explanation of why this log text maps to the given severity",
    )
    created_at: int = Field(
        ...,
        description="Epoch milliseconds when the example was created",
    )
    updated_at: int = Field(
        ...,
        description="Epoch milliseconds when the example was last updated",
    )
