# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Log Ingestion Agent for the EKS Log Alerts pattern.

Polls monitored CloudWatch log groups for error patterns (ERROR, FATAL,
Exception, Traceback), extracts structured error data into ExtractedError
models, and tracks last-polled timestamps per log group to avoid
reprocessing events.

If a monitored log group does not exist, the agent logs a warning and
continues polling the remaining groups (Requirement 1.4).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from models import ExtractedError

logger: logging.Logger = logging.getLogger(__name__)

# Regex pattern that matches any of the four error indicators.
# Used to filter CloudWatch log events for error content.
ERROR_PATTERN: re.Pattern[str] = re.compile(
    r"\bERROR\b|\bFATAL\b|\bException\b|\bTraceback\b",
    re.IGNORECASE,
)

# The CloudWatch Logs filterPattern string used in the filter_log_events
# API call. This is a CloudWatch-native filter, not a Python regex.
# It matches events containing any of the four error keywords.
CLOUDWATCH_FILTER_PATTERN: str = '?"ERROR" ?"FATAL" ?"Exception" ?"Traceback"'


def extract_application_name(log_group_name: str, log_stream_name: str) -> str:
    """Derive an application name from the log group or stream metadata.

    Attempts to extract a meaningful application identifier by parsing the
    log group name. Falls back to the log stream name if the log group
    does not contain a recognisable application segment.

    Heuristic:
        1. Split the log group name on '/' and look for the segment after
           a known prefix token (e.g. "eks", "containers").
        2. If no recognisable segment is found, use the last non-empty
           segment of the log group name.
        3. If the log group name yields nothing useful, fall back to the
           first segment of the log stream name (split on '/').

    Args:
        log_group_name: Full CloudWatch log group name,
            e.g. "/aws/eks/team5-app/containers".
        log_stream_name: CloudWatch log stream name,
            e.g. "pod-abc123/container-xyz".

    Returns:
        A non-empty string representing the application name.
    """
    # Split log group into segments, filtering out empty strings from
    # leading slashes.
    segments: List[str] = [s for s in log_group_name.split("/") if s]

    # Look for a segment immediately after known prefix tokens.
    prefix_tokens: List[str] = ["eks", "ecs", "lambda"]
    for i, segment in enumerate(segments):
        if segment.lower() in prefix_tokens and i + 1 < len(segments):
            return segments[i + 1]

    # Fall back to the last non-empty segment of the log group name.
    if segments:
        return segments[-1]

    # Final fallback: first segment of the log stream name.
    stream_segments: List[str] = [s for s in log_stream_name.split("/") if s]
    if stream_segments:
        return stream_segments[0]

    # Should not happen with valid CloudWatch data, but guard against it.
    return "unknown"


def extract_error(
    event: Dict,
    log_group_name: str,
    log_stream_name: str,
) -> Optional[ExtractedError]:
    """Extract structured error data from a single CloudWatch log event.

    Parses the event dict returned by the CloudWatch Logs
    ``filter_log_events`` API and produces an ``ExtractedError`` if the
    event message matches one of the known error patterns.

    Args:
        event: A single event dict from the CloudWatch Logs API response.
            Expected keys: ``timestamp`` (epoch ms), ``message`` (str),
            and optionally ``logStreamName`` (str).
        log_group_name: The CloudWatch log group this event belongs to.
        log_stream_name: The CloudWatch log stream this event belongs to.
            Used as a fallback if the event dict does not contain
            ``logStreamName``.

    Returns:
        An ``ExtractedError`` instance if the event message matches an
        error pattern, or ``None`` if the message does not contain a
        recognised error indicator.
    """
    message: str = event.get("message", "")
    if not message or not ERROR_PATTERN.search(message):
        return None

    # CloudWatch timestamps are epoch milliseconds.
    epoch_ms: int = event.get("timestamp", 0)
    iso_timestamp: str = datetime.fromtimestamp(
        epoch_ms / 1000.0,
        tz=timezone.utc,
    ).isoformat()

    # Prefer the stream name embedded in the event; fall back to the
    # one provided by the caller (from the API request context).
    resolved_stream: str = event.get("logStreamName", log_stream_name)

    application_name: str = extract_application_name(
        log_group_name=log_group_name,
        log_stream_name=resolved_stream,
    )

    return ExtractedError(
        timestamp=iso_timestamp,
        log_group_name=log_group_name,
        log_stream_name=resolved_stream,
        application_name=application_name,
        error_message=message.strip(),
    )


class LogIngestionAgent:
    """Agent that polls CloudWatch log groups for error events.

    Continuously monitors the log groups specified in the application
    configuration, detects error patterns, and extracts structured error
    data for downstream classification.

    The agent tracks the last-polled timestamp for each log group so that
    subsequent polls only retrieve new events, preventing reprocessing.

    Args:
        monitored_log_groups: List of CloudWatch log group names to poll.
        poll_interval_seconds: How often (in seconds) to poll for new
            events. Used by the caller to schedule repeated invocations;
            stored here for reference.
        cloudwatch_client: An optional pre-configured boto3 CloudWatch
            Logs client. If ``None``, a new client is created using the
            default boto3 session.

    Attributes:
        monitored_log_groups: The log group names being monitored.
        poll_interval_seconds: The configured polling interval.
        _last_polled_timestamps: Mapping of log group name to the epoch-ms
            timestamp of the most recent event seen in that group.
        _client: The boto3 CloudWatch Logs client used for API calls.
    """

    def __init__(
        self,
        monitored_log_groups: List[str],
        poll_interval_seconds: int,
        cloudwatch_client: Optional[object] = None,
    ) -> None:
        """Initialise the LogIngestionAgent.

        Args:
            monitored_log_groups: List of CloudWatch log group names to
                monitor for error events.
            poll_interval_seconds: Polling interval in seconds.
            cloudwatch_client: Optional pre-configured boto3 CloudWatch
                Logs client. A new default client is created if not
                provided.
        """
        self.monitored_log_groups: List[str] = monitored_log_groups
        self.poll_interval_seconds: int = poll_interval_seconds

        # Tracks the epoch-ms timestamp of the last event processed per
        # log group. Initialised to 0 so the first poll retrieves all
        # available events.
        self._last_polled_timestamps: Dict[str, int] = {
            group: 0 for group in monitored_log_groups
        }

        # Allow dependency injection of the CloudWatch client for testing.
        if cloudwatch_client is not None:
            self._client = cloudwatch_client
        else:
            self._client = boto3.client("logs")

    def _poll_log_group(self, log_group_name: str) -> List[ExtractedError]:
        """Poll a single CloudWatch log group for new error events.

        Uses ``filter_log_events`` with the CloudWatch-native filter
        pattern and a ``startTime`` equal to the last-polled timestamp
        plus one millisecond (to avoid re-fetching the last seen event).

        If the log group does not exist, a warning is logged and an empty
        list is returned (Requirement 1.4).

        Args:
            log_group_name: The CloudWatch log group name to poll.

        Returns:
            A list of ``ExtractedError`` instances extracted from newly
            detected error events. May be empty if no new errors are found
            or if the log group does not exist.
        """
        errors: List[ExtractedError] = []
        start_time: int = self._last_polled_timestamps.get(log_group_name, 0)

        # Add 1 ms to avoid re-fetching the exact last event we already
        # processed. On the very first poll (start_time == 0) we fetch
        # everything available.
        if start_time > 0:
            start_time += 1

        try:
            # Use a paginator-style loop to handle truncated responses.
            next_token: Optional[str] = None
            max_event_timestamp: int = self._last_polled_timestamps.get(
                log_group_name, 0
            )

            while True:
                kwargs: Dict = {
                    "logGroupName": log_group_name,
                    "filterPattern": CLOUDWATCH_FILTER_PATTERN,
                    "startTime": start_time,
                    "interleaved": True,
                }
                if next_token is not None:
                    kwargs["nextToken"] = next_token

                response: Dict = self._client.filter_log_events(**kwargs)

                for event in response.get("events", []):
                    extracted: Optional[ExtractedError] = extract_error(
                        event=event,
                        log_group_name=log_group_name,
                        log_stream_name=event.get("logStreamName", "unknown"),
                    )
                    if extracted is not None:
                        errors.append(extracted)

                    # Track the highest timestamp we've seen so far.
                    event_ts: int = event.get("timestamp", 0)
                    if event_ts > max_event_timestamp:
                        max_event_timestamp = event_ts

                # Check for pagination.
                next_token = response.get("nextToken")
                if not next_token:
                    break

            # Update the last-polled timestamp for this log group.
            if max_event_timestamp > self._last_polled_timestamps.get(
                log_group_name, 0
            ):
                self._last_polled_timestamps[log_group_name] = max_event_timestamp

        except ClientError as exc:
            error_code: str = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "CloudWatch log group '%s' does not exist. "
                    "Skipping and continuing with remaining groups.",
                    log_group_name,
                )
            else:
                # Re-raise unexpected AWS errors so they surface clearly.
                raise

        return errors

    def poll_and_extract(self) -> List[ExtractedError]:
        """Poll all monitored log groups and return extracted error entries.

        Iterates over every configured log group, polls for new error
        events since the last poll, and aggregates the results. Log groups
        that do not exist are skipped with a warning (Requirement 1.4).

        Returns:
            A list of ``ExtractedError`` instances from all monitored log
            groups combined. May be empty if no new errors are detected.
        """
        all_errors: List[ExtractedError] = []

        for log_group_name in self.monitored_log_groups:
            group_errors: List[ExtractedError] = self._poll_log_group(
                log_group_name=log_group_name,
            )
            all_errors.extend(group_errors)

        if all_errors:
            logger.info(
                "Extracted %d error(s) from %d monitored log group(s).",
                len(all_errors),
                len(self.monitored_log_groups),
            )
        else:
            logger.debug(
                "No new errors detected across %d monitored log group(s).",
                len(self.monitored_log_groups),
            )

        return all_errors

    def get_last_polled_timestamp(self, log_group_name: str) -> int:
        """Return the last-polled epoch-ms timestamp for a log group.

        Useful for inspecting agent state in tests and trace records.

        Args:
            log_group_name: The CloudWatch log group name to query.

        Returns:
            The epoch-millisecond timestamp of the most recent event
            processed from the given log group, or 0 if no events have
            been processed yet.
        """
        return self._last_polled_timestamps.get(log_group_name, 0)
