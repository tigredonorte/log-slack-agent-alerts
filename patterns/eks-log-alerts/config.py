# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Environment variable configuration loader for the EKS Log Alerts pattern.

Reads all required environment variables at startup and exposes them as a
strongly-typed dataclass. If any required variable is absent the loader
fails immediately with a descriptive error naming the missing variable,
satisfying Requirements 9.1 and 9.2.

Required environment variables:
    SLACK_CHANNEL_WEBHOOK_URL       – Slack incoming webhook URL
    MONITORED_LOG_GROUPS            – Comma-separated CloudWatch log group names
    CONFIDENCE_THRESHOLD            – Float classification confidence threshold
    LOG_POLL_INTERVAL_SECONDS       – Integer polling interval in seconds
    CLASSIFICATION_MODEL_ID         – Bedrock model ID for classification
    SEVERITY_EXAMPLES_TABLE_NAME    – DynamoDB table name for severity examples
    PREFIX                          – Resource name prefix (e.g. "team5")

Validates: Requirements 9.1, 9.2
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List


# All environment variable names that MUST be present at startup.
REQUIRED_ENV_VARS: List[str] = [
    "SLACK_CHANNEL_WEBHOOK_URL",
    "MONITORED_LOG_GROUPS",
    "CONFIDENCE_THRESHOLD",
    "LOG_POLL_INTERVAL_SECONDS",
    "CLASSIFICATION_MODEL_ID",
    "SEVERITY_EXAMPLES_TABLE_NAME",
    "PREFIX",
]


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration populated from environment variables.

    Attributes:
        slack_channel_webhook_url: Slack incoming webhook URL for posting alerts.
        monitored_log_groups: List of CloudWatch log group names to monitor.
        confidence_threshold: Minimum confidence score (0.0–1.0) for
            auto-classification; below this the entry is held for human review.
        log_poll_interval_seconds: How often (in seconds) the
            Log_Ingestion_Agent polls CloudWatch for new error events.
        classification_model_id: Amazon Bedrock model identifier used by
            the Classification_Agent.
        severity_examples_table_name: DynamoDB table name that stores
            admin-curated severity examples for few-shot classification.
        prefix: String prepended to all AWS resource names (e.g. "team5").
    """

    slack_channel_webhook_url: str
    monitored_log_groups: List[str]
    confidence_threshold: float
    log_poll_interval_seconds: int
    classification_model_id: str
    severity_examples_table_name: str
    prefix: str


def _get_required_env(name: str) -> str:
    """Read a required environment variable or fail with a descriptive error.

    Args:
        name: The environment variable name to look up.

    Returns:
        The string value of the environment variable.

    Raises:
        EnvironmentError: If the variable is not set or is empty, with a
            message that names the missing variable.
    """
    value: str | None = os.environ.get(name)
    if value is None or value.strip() == "":
        raise EnvironmentError(
            f"Required environment variable '{name}' is missing. "
            f"Set {name} before starting the application."
        )
    return value


def _parse_monitored_log_groups(raw: str) -> List[str]:
    """Parse a comma-separated string into a list of log group names.

    Leading/trailing whitespace around each entry is stripped. Empty entries
    that result from extra commas are silently discarded.

    Args:
        raw: Comma-separated string of CloudWatch log group names,
            e.g. "/aws/eks/app1,/aws/eks/app2".

    Returns:
        A list of non-empty, stripped log group name strings.

    Raises:
        ValueError: If the parsed list is empty (no valid log group names).
    """
    groups: List[str] = [g.strip() for g in raw.split(",") if g.strip()]
    if not groups:
        raise ValueError(
            "MONITORED_LOG_GROUPS must contain at least one non-empty log group name."
        )
    return groups


def _parse_confidence_threshold(raw: str) -> float:
    """Parse the confidence threshold string into a float.

    Args:
        raw: String representation of a float, e.g. "0.7".

    Returns:
        The parsed float value.

    Raises:
        ValueError: If the string cannot be parsed as a float or is outside
            the valid range [0.0, 1.0].
    """
    try:
        value: float = float(raw)
    except ValueError:
        raise ValueError(
            f"CONFIDENCE_THRESHOLD must be a valid float, got '{raw}'."
        )
    if value < 0.0 or value > 1.0:
        raise ValueError(
            f"CONFIDENCE_THRESHOLD must be between 0.0 and 1.0, got {value}."
        )
    return value


def _parse_log_poll_interval(raw: str) -> int:
    """Parse the log poll interval string into a positive integer.

    Args:
        raw: String representation of an integer, e.g. "30".

    Returns:
        The parsed integer value.

    Raises:
        ValueError: If the string cannot be parsed as an integer or is not
            a positive number.
    """
    try:
        value: int = int(raw)
    except ValueError:
        raise ValueError(
            f"LOG_POLL_INTERVAL_SECONDS must be a valid integer, got '{raw}'."
        )
    if value <= 0:
        raise ValueError(
            f"LOG_POLL_INTERVAL_SECONDS must be a positive integer, got {value}."
        )
    return value


def load_config() -> AppConfig:
    """Load and validate all required environment variables into an AppConfig.

    Reads every required environment variable, parses typed fields
    (float, int, comma-separated list), and returns a frozen dataclass.
    Fails immediately with a descriptive error if any variable is missing
    or has an invalid value.

    Returns:
        A fully populated, immutable AppConfig instance.

    Raises:
        EnvironmentError: If any required environment variable is missing.
        ValueError: If a typed variable cannot be parsed or is out of range.
    """
    # Step 1: Read all required raw string values — fail fast on any missing var
    raw_values: dict[str, str] = {}
    for var_name in REQUIRED_ENV_VARS:
        raw_values[var_name] = _get_required_env(name=var_name)

    # Step 2: Parse typed fields
    monitored_log_groups: List[str] = _parse_monitored_log_groups(
        raw=raw_values["MONITORED_LOG_GROUPS"]
    )
    confidence_threshold: float = _parse_confidence_threshold(
        raw=raw_values["CONFIDENCE_THRESHOLD"]
    )
    log_poll_interval_seconds: int = _parse_log_poll_interval(
        raw=raw_values["LOG_POLL_INTERVAL_SECONDS"]
    )

    # Step 3: Construct and return the immutable config
    return AppConfig(
        slack_channel_webhook_url=raw_values["SLACK_CHANNEL_WEBHOOK_URL"],
        monitored_log_groups=monitored_log_groups,
        confidence_threshold=confidence_threshold,
        log_poll_interval_seconds=log_poll_interval_seconds,
        classification_model_id=raw_values["CLASSIFICATION_MODEL_ID"],
        severity_examples_table_name=raw_values["SEVERITY_EXAMPLES_TABLE_NAME"],
        prefix=raw_values["PREFIX"],
    )
