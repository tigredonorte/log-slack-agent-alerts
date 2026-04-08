# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property-based tests for classification output well-formedness (Property 2)
and low-confidence escalation (Property 3).

Feature: eks-log-slack-alerts, Property 2: Classification output well-formedness

For any error log entry, the returned ``ClassificationResult`` SHALL have
severity in {low, medium, high, critical}, confidence_score in [0.0, 1.0],
and non-empty rationale.

Feature: eks-log-slack-alerts, Property 3: Low-confidence and ambiguous
classification escalation

For any classification with confidence below threshold OR two severities
within 0.1, status SHALL be ``awaiting_review`` and ``candidate_severities``
SHALL contain at least two entries.

Uses Hypothesis to generate random error entries and mock LLM responses,
then verifies the ClassificationAgent produces well-formed output in all
cases.

Validates: Requirements 2.1, 2.3, 2.4, 2.6, 8.1, 8.2, 8.3
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from hypothesis import given, settings, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import modules from the pattern directory.
# ---------------------------------------------------------------------------
_PATTERN_DIR: Path = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
_AGENTS_DIR: Path = _PATTERN_DIR / "agents"

for _dir in (_PATTERN_DIR, _AGENTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

_classification_module = importlib.import_module("classification_agent")
_models_module = importlib.import_module("models")

ClassificationAgent = _classification_module.ClassificationAgent
ExtractedError = _models_module.ExtractedError
ClassificationResult = _models_module.ClassificationResult

VALID_SEVERITIES: List[str] = ["low", "medium", "high", "critical"]


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for generating a realistic ExtractedError.
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

# Strategy for generating a valid confidence score.
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


def _build_mock_llm_response(
    severity: str,
    confidence_score: float,
    rationale: str,
    all_candidates: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build a mock LLM JSON response string.

    Args:
        severity: The severity level to include.
        confidence_score: The confidence score to include.
        rationale: The rationale text to include.
        all_candidates: Optional list of candidate dicts.

    Returns:
        A JSON string matching the expected LLM response format.
    """
    response: Dict[str, Any] = {
        "severity": severity,
        "confidence_score": confidence_score,
        "rationale": rationale,
    }
    if all_candidates is not None:
        response["all_candidates"] = all_candidates
    return json.dumps(response)


def _make_mock_dynamodb_resource(
    examples: Optional[List[Dict[str, str]]] = None,
) -> MagicMock:
    """Create a mock DynamoDB resource that returns canned examples.

    Args:
        examples: Optional list of severity example dicts. Defaults to
            an empty list.

    Returns:
        A MagicMock configured as a boto3 DynamoDB resource.
    """
    if examples is None:
        examples = []

    mock_resource: MagicMock = MagicMock()
    mock_table: MagicMock = MagicMock()
    mock_table.scan.return_value = {"Items": examples}
    mock_resource.Table.return_value = mock_table
    return mock_resource


def _make_mock_bedrock_client(response_text: str) -> MagicMock:
    """Create a mock Bedrock Runtime client that returns a canned response.

    Args:
        response_text: The text content to return from the model.

    Returns:
        A MagicMock configured as a boto3 Bedrock Runtime client.
    """
    mock_client: MagicMock = MagicMock()
    mock_client.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": response_text}],
            }
        }
    }
    return mock_client


# ---------------------------------------------------------------------------
# Property 2: Classification output well-formedness
# ---------------------------------------------------------------------------


class TestClassificationOutputWellFormedness:
    """Property 2: Classification output well-formedness.

    For any error log entry, the returned ClassificationResult SHALL have
    severity in {low, medium, high, critical}, confidence_score in
    [0.0, 1.0], and non-empty rationale.
    """

    @given(
        error=extracted_error_strategy,
        severity=severity_strategy,
        confidence=confidence_strategy,
        rationale=rationale_strategy,
    )
    @settings(max_examples=100)
    def test_well_formed_output_for_any_valid_llm_response(
        self,
        error: ExtractedError,
        severity: str,
        confidence: float,
        rationale: str,
    ) -> None:
        """For any valid LLM response, the ClassificationResult has a
        valid severity, confidence in [0.0, 1.0], and non-empty rationale.

        Args:
            error: Random ExtractedError generated by Hypothesis.
            severity: Random valid severity level.
            confidence: Random confidence score in [0.0, 1.0].
            rationale: Random non-empty rationale string.
        """
        # Build candidates with well-separated scores to avoid escalation.
        all_candidates: List[Dict[str, Any]] = [
            {
                "severity": severity,
                "confidence_score": confidence,
                "rationale": rationale,
            },
            {
                "severity": "low" if severity != "low" else "medium",
                "confidence_score": max(0.0, confidence - 0.5),
                "rationale": "Alternative candidate.",
            },
        ]

        mock_response: str = _build_mock_llm_response(
            severity=severity,
            confidence_score=confidence,
            rationale=rationale,
            all_candidates=all_candidates,
        )

        agent: ClassificationAgent = ClassificationAgent(
            model_id="test-model",
            confidence_threshold=0.7,
            severity_examples_table_name="test-table",
            dynamodb_resource=_make_mock_dynamodb_resource(),
            bedrock_client=_make_mock_bedrock_client(response_text=mock_response),
        )

        result: ClassificationResult = agent.classify(error=error)

        # Property 2 assertions: well-formedness.
        assert result.severity in VALID_SEVERITIES, (
            f"severity must be one of {VALID_SEVERITIES}, got: {result.severity!r}"
        )
        assert 0.0 <= result.confidence_score <= 1.0, (
            f"confidence_score must be in [0.0, 1.0], got: {result.confidence_score}"
        )
        assert result.rationale and result.rationale.strip(), (
            f"rationale must be non-empty, got: {result.rationale!r}"
        )
        assert result.status in ("classified", "awaiting_review"), (
            f"status must be 'classified' or 'awaiting_review', got: {result.status!r}"
        )

    @given(
        error=extracted_error_strategy,
        rationale=rationale_strategy,
    )
    @settings(max_examples=100)
    def test_well_formed_output_for_unparseable_response(
        self,
        error: ExtractedError,
        rationale: str,
    ) -> None:
        """When the LLM returns unparseable text (both attempts), the
        result should still be well-formed with awaiting_review status.

        Args:
            error: Random ExtractedError generated by Hypothesis.
            rationale: Random rationale (unused, just for variety).
        """
        # Return garbage text that cannot be parsed as JSON.
        mock_client: MagicMock = _make_mock_bedrock_client(
            response_text="This is not valid JSON at all."
        )

        agent: ClassificationAgent = ClassificationAgent(
            model_id="test-model",
            confidence_threshold=0.7,
            severity_examples_table_name="test-table",
            dynamodb_resource=_make_mock_dynamodb_resource(),
            bedrock_client=mock_client,
        )

        result: ClassificationResult = agent.classify(error=error)

        # Even with unparseable responses, output must be well-formed.
        assert result.severity in VALID_SEVERITIES
        assert 0.0 <= result.confidence_score <= 1.0
        assert result.rationale and result.rationale.strip()
        assert result.status == "awaiting_review"



# ---------------------------------------------------------------------------
# Property 3: Low-confidence and ambiguous classification escalation
# ---------------------------------------------------------------------------


class TestLowConfidenceEscalation:
    """Property 3: Low-confidence and ambiguous classification escalation.

    For any classification with confidence below threshold OR two
    severities within 0.1, status SHALL be ``awaiting_review`` and
    ``candidate_severities`` SHALL contain at least two entries.
    """

    @given(
        error=extracted_error_strategy,
        severity=severity_strategy,
        rationale=rationale_strategy,
        low_confidence=st.floats(
            min_value=0.0,
            max_value=0.69,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(max_examples=100)
    def test_low_confidence_triggers_awaiting_review(
        self,
        error: ExtractedError,
        severity: str,
        rationale: str,
        low_confidence: float,
    ) -> None:
        """When confidence is below the threshold (0.7), the result
        status SHALL be ``awaiting_review`` and candidate_severities
        SHALL contain at least two entries.

        Args:
            error: Random ExtractedError generated by Hypothesis.
            severity: Random valid severity level.
            rationale: Random non-empty rationale string.
            low_confidence: Random confidence below 0.7 threshold.
        """
        # Build candidates with well-separated scores (no ambiguity),
        # so only the low-confidence condition triggers escalation.
        all_candidates: List[Dict[str, Any]] = [
            {
                "severity": severity,
                "confidence_score": low_confidence,
                "rationale": rationale,
            },
            {
                "severity": "low" if severity != "low" else "medium",
                "confidence_score": max(0.0, low_confidence - 0.3),
                "rationale": "Much lower alternative.",
            },
        ]

        mock_response: str = _build_mock_llm_response(
            severity=severity,
            confidence_score=low_confidence,
            rationale=rationale,
            all_candidates=all_candidates,
        )

        agent: ClassificationAgent = ClassificationAgent(
            model_id="test-model",
            confidence_threshold=0.7,
            severity_examples_table_name="test-table",
            dynamodb_resource=_make_mock_dynamodb_resource(),
            bedrock_client=_make_mock_bedrock_client(response_text=mock_response),
        )

        result: ClassificationResult = agent.classify(error=error)

        # Property 3 assertions: low confidence escalation.
        assert result.status == "awaiting_review", (
            f"Expected status 'awaiting_review' for confidence {low_confidence:.2f} "
            f"(threshold 0.7), got: {result.status!r}"
        )
        assert result.candidate_severities is not None, (
            "candidate_severities must not be None when status is awaiting_review"
        )
        assert len(result.candidate_severities) >= 2, (
            f"candidate_severities must have at least 2 entries, "
            f"got {len(result.candidate_severities)}"
        )

    @given(
        error=extracted_error_strategy,
        severity=severity_strategy,
        rationale=rationale_strategy,
        base_confidence=st.floats(
            min_value=0.7,
            max_value=0.95,
            allow_nan=False,
            allow_infinity=False,
        ),
        delta=st.floats(
            min_value=0.0,
            max_value=0.1,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(max_examples=100)
    def test_ambiguous_candidates_trigger_awaiting_review(
        self,
        error: ExtractedError,
        severity: str,
        rationale: str,
        base_confidence: float,
        delta: float,
    ) -> None:
        """When two candidate severities have scores within 0.1 of each
        other, the result status SHALL be ``awaiting_review`` and
        candidate_severities SHALL contain at least two entries.

        Args:
            error: Random ExtractedError generated by Hypothesis.
            severity: Random valid severity level.
            rationale: Random non-empty rationale string.
            base_confidence: Random confidence above threshold.
            delta: Random delta within [0.0, 0.1] for ambiguity.
        """
        # Pick a second severity different from the first.
        other_severity: str = "low" if severity != "low" else "medium"
        second_confidence: float = min(1.0, base_confidence - delta)

        all_candidates: List[Dict[str, Any]] = [
            {
                "severity": severity,
                "confidence_score": base_confidence,
                "rationale": rationale,
            },
            {
                "severity": other_severity,
                "confidence_score": second_confidence,
                "rationale": "Close alternative severity.",
            },
        ]

        mock_response: str = _build_mock_llm_response(
            severity=severity,
            confidence_score=base_confidence,
            rationale=rationale,
            all_candidates=all_candidates,
        )

        agent: ClassificationAgent = ClassificationAgent(
            model_id="test-model",
            confidence_threshold=0.7,
            severity_examples_table_name="test-table",
            dynamodb_resource=_make_mock_dynamodb_resource(),
            bedrock_client=_make_mock_bedrock_client(response_text=mock_response),
        )

        result: ClassificationResult = agent.classify(error=error)

        # Property 3 assertions: ambiguous classification escalation.
        assert result.status == "awaiting_review", (
            f"Expected status 'awaiting_review' for ambiguous candidates "
            f"(scores {base_confidence:.2f} and {second_confidence:.2f}, "
            f"delta {delta:.2f}), got: {result.status!r}"
        )
        assert result.candidate_severities is not None, (
            "candidate_severities must not be None when status is awaiting_review"
        )
        assert len(result.candidate_severities) >= 2, (
            f"candidate_severities must have at least 2 entries, "
            f"got {len(result.candidate_severities)}"
        )

    @given(
        error=extracted_error_strategy,
        severity=severity_strategy,
        rationale=rationale_strategy,
        high_confidence=st.floats(
            min_value=0.7,
            max_value=1.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(max_examples=100)
    def test_high_confidence_non_ambiguous_is_classified(
        self,
        error: ExtractedError,
        severity: str,
        rationale: str,
        high_confidence: float,
    ) -> None:
        """When confidence is at or above threshold and candidates are
        NOT ambiguous, the result status SHALL be ``classified``.

        This is the inverse property: verifying that escalation only
        happens when the conditions are met.

        Args:
            error: Random ExtractedError generated by Hypothesis.
            severity: Random valid severity level.
            rationale: Random non-empty rationale string.
            high_confidence: Random confidence at or above 0.7 threshold.
        """
        # Build candidates with well-separated scores (> 0.1 apart).
        all_candidates: List[Dict[str, Any]] = [
            {
                "severity": severity,
                "confidence_score": high_confidence,
                "rationale": rationale,
            },
            {
                "severity": "low" if severity != "low" else "medium",
                "confidence_score": max(0.0, high_confidence - 0.5),
                "rationale": "Much lower alternative.",
            },
        ]

        mock_response: str = _build_mock_llm_response(
            severity=severity,
            confidence_score=high_confidence,
            rationale=rationale,
            all_candidates=all_candidates,
        )

        agent: ClassificationAgent = ClassificationAgent(
            model_id="test-model",
            confidence_threshold=0.7,
            severity_examples_table_name="test-table",
            dynamodb_resource=_make_mock_dynamodb_resource(),
            bedrock_client=_make_mock_bedrock_client(response_text=mock_response),
        )

        result: ClassificationResult = agent.classify(error=error)

        # Inverse property: high confidence + non-ambiguous = classified.
        assert result.status == "classified", (
            f"Expected status 'classified' for confidence {high_confidence:.2f} "
            f"(threshold 0.7) with non-ambiguous candidates, got: {result.status!r}"
        )
