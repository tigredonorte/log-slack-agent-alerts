# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Classification Agent for the EKS Log Alerts pattern.

Classifies error log entries into severity levels (low, medium, high,
critical) using an LLM with few-shot examples retrieved from DynamoDB.
Implements low-confidence escalation: if confidence is below the
configured threshold OR two candidate severities are within 0.1 of each
other, the entry is flagged as ``awaiting_review`` with at least two
candidate severities populated.

Handles unparseable LLM responses by retrying once with a stricter
prompt, then flagging as ``awaiting_review`` if still unparseable.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 8.1, 8.2, 8.3
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import boto3

from models import ClassificationResult, ExtractedError, SeverityLevel

logger: logging.Logger = logging.getLogger(__name__)

# Valid severity levels for validation of LLM output.
VALID_SEVERITIES: List[str] = ["low", "medium", "high", "critical"]

# Maximum number of retry attempts for unparseable LLM responses.
MAX_LLM_RETRIES: int = 1


class ClassificationAgent:
    """Agent that classifies error log entries by severity using LLM.

    Retrieves few-shot examples from a DynamoDB table, constructs a
    classification prompt, invokes a Bedrock model, and parses the
    response into a ``ClassificationResult``. Implements escalation
    logic for low-confidence or ambiguous classifications.

    Args:
        model_id: Amazon Bedrock model identifier for classification.
        confidence_threshold: Minimum confidence score (0.0–1.0) for
            auto-classification. Below this, entries are held for review.
        severity_examples_table_name: DynamoDB table name storing
            admin-curated severity examples.
        dynamodb_resource: Optional pre-configured boto3 DynamoDB
            resource. If ``None``, a new resource is created.
        bedrock_client: Optional pre-configured boto3 Bedrock Runtime
            client. If ``None``, a new client is created.
        notification_callback: Optional callable invoked when a critical
            error is classified with sufficient confidence. Receives the
            ``ExtractedError`` and ``ClassificationResult`` as arguments.

    Attributes:
        model_id: The Bedrock model ID used for classification.
        confidence_threshold: The configured confidence threshold.
        severity_examples_table_name: The DynamoDB table name.
    """

    def __init__(
        self,
        model_id: str,
        confidence_threshold: float,
        severity_examples_table_name: str,
        dynamodb_resource: Optional[Any] = None,
        bedrock_client: Optional[Any] = None,
        notification_callback: Optional[Any] = None,
    ) -> None:
        """Initialise the ClassificationAgent.

        Args:
            model_id: Amazon Bedrock model identifier for classification.
            confidence_threshold: Minimum confidence score for
                auto-classification.
            severity_examples_table_name: DynamoDB table name for
                severity examples.
            dynamodb_resource: Optional pre-configured boto3 DynamoDB
                resource for dependency injection in tests.
            bedrock_client: Optional pre-configured boto3 Bedrock Runtime
                client for dependency injection in tests.
            notification_callback: Optional callable invoked for critical
                errors that pass the confidence threshold. Signature:
                ``(error: ExtractedError, result: ClassificationResult) -> None``.
        """
        self.model_id: str = model_id
        self.confidence_threshold: float = confidence_threshold
        self.severity_examples_table_name: str = severity_examples_table_name
        self._notification_callback = notification_callback

        # Allow dependency injection for testing.
        if dynamodb_resource is not None:
            self._dynamodb = dynamodb_resource
        else:
            self._dynamodb = boto3.resource("dynamodb")

        if bedrock_client is not None:
            self._bedrock_client = bedrock_client
        else:
            self._bedrock_client = boto3.client("bedrock-runtime")


    def _fetch_severity_examples(self) -> List[Dict[str, str]]:
        """Retrieve all severity examples from the DynamoDB table.

        Scans the Severity_Examples_Store table and returns a list of
        example dicts, each containing ``severity``, ``logText``, and
        optionally ``description``.

        Returns:
            A list of dicts with keys ``severity``, ``logText``, and
            ``description`` (if present). Returns an empty list if the
            table is empty or an error occurs.

        Raises:
            Exception: Re-raises any DynamoDB error after logging it.
        """
        table = self._dynamodb.Table(self.severity_examples_table_name)

        try:
            response: Dict = table.scan()
            items: List[Dict] = response.get("Items", [])

            # Handle pagination for large tables.
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                items.extend(response.get("Items", []))

            logger.info(
                "Retrieved %d severity example(s) from DynamoDB table '%s'.",
                len(items),
                self.severity_examples_table_name,
            )
            return items

        except Exception:
            logger.exception(
                "Failed to retrieve severity examples from DynamoDB table '%s'.",
                self.severity_examples_table_name,
            )
            raise

    def _build_classification_prompt(
        self,
        error: ExtractedError,
        examples: List[Dict[str, str]],
        strict: bool = False,
    ) -> str:
        """Construct the few-shot classification prompt for the LLM.

        Builds a prompt that includes admin-curated severity examples as
        few-shot context, followed by the target error log entry. When
        ``strict`` is True, the prompt includes additional formatting
        constraints to help the LLM produce parseable output.

        Args:
            error: The extracted error entry to classify.
            examples: List of severity example dicts from DynamoDB, each
                containing ``severity`` and ``logText`` keys.
            strict: If True, adds stricter formatting instructions to the
                prompt. Used on retry after an unparseable response.

        Returns:
            The complete prompt string to send to the LLM.
        """
        # Build the few-shot examples section.
        examples_text: str = ""
        for ex in examples:
            severity: str = ex.get("severity", "unknown")
            log_text: str = ex.get("logText", "")
            description: str = ex.get("description", "")
            examples_text += f"Log: {log_text}\nSeverity: {severity}"
            if description:
                examples_text += f"\nReason: {description}"
            examples_text += "\n\n"

        strict_instructions: str = ""
        if strict:
            strict_instructions = (
                "\nCRITICAL: You MUST respond with ONLY a valid JSON object. "
                "Do NOT include any text before or after the JSON. "
                "Do NOT use markdown code blocks. "
                "The JSON must have exactly these keys: "
                '"severity", "confidence_score", "rationale".\n'
            )

        prompt: str = f"""You are an expert DevOps engineer classifying EKS application errors by severity.

Classify the following error log entry into exactly one severity level: low, medium, high, or critical.

Severity definitions:
- low: Minor issues that do not affect service availability (e.g., deprecation warnings, non-critical config issues)
- medium: Issues that may degrade performance but service remains available (e.g., high memory usage, slow queries)
- high: Issues that affect service availability for some users (e.g., connection failures, pod restarts)
- critical: Issues that cause complete service outage or data loss (e.g., out of memory, all retries exhausted, node failures)

{examples_text}Now classify this error:

Application: {error.application_name}
Log Group: {error.log_group_name}
Timestamp: {error.timestamp}
Error Log:
{error.error_message}
{strict_instructions}
Respond with a JSON object containing exactly these fields:
- "severity": one of "low", "medium", "high", "critical"
- "confidence_score": a float between 0.0 and 1.0
- "rationale": a brief 1-2 sentence explanation of why this severity was chosen

Also include an "all_candidates" array with objects for each severity level you considered, each having "severity", "confidence_score", and "rationale" fields. Order by confidence_score descending.

Example response format:
{{"severity": "high", "confidence_score": 0.85, "rationale": "Connection refused errors indicate service unavailability.", "all_candidates": [{{"severity": "high", "confidence_score": 0.85, "rationale": "Connection refused errors indicate service unavailability."}}, {{"severity": "critical", "confidence_score": 0.75, "rationale": "Could escalate to full outage."}}]}}"""

        return prompt

    def _invoke_bedrock_model(self, prompt: str) -> str:
        """Invoke the Bedrock model with the given prompt and return the response text.

        Uses the Bedrock Runtime ``converse`` API to send the prompt and
        retrieve the model's text response.

        Args:
            prompt: The complete classification prompt to send.

        Returns:
            The raw text response from the model.

        Raises:
            Exception: Re-raises any Bedrock API error after logging it.
        """
        try:
            response: Dict = self._bedrock_client.converse(
                modelId=self.model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": 1024,
                    "temperature": 0.1,
                },
            )

            # Extract the text from the response.
            output_message: Dict = response.get("output", {}).get("message", {})
            content_blocks: List[Dict] = output_message.get("content", [])

            for block in content_blocks:
                if "text" in block:
                    return block["text"]

            logger.error("Bedrock response contained no text content blocks.")
            return ""

        except Exception:
            logger.exception(
                "Failed to invoke Bedrock model '%s'.",
                self.model_id,
            )
            raise


    def _parse_llm_response(self, raw_response: str) -> Optional[Dict[str, Any]]:
        """Parse the raw LLM response text into a structured dict.

        Attempts to extract a JSON object from the response. Handles
        cases where the JSON is wrapped in markdown code blocks or
        surrounded by extra text.

        Args:
            raw_response: The raw text response from the LLM.

        Returns:
            A dict with keys ``severity``, ``confidence_score``,
            ``rationale``, and optionally ``all_candidates``, or ``None``
            if the response cannot be parsed.
        """
        if not raw_response or not raw_response.strip():
            return None

        text: str = raw_response.strip()

        # Strip markdown code block wrappers if present.
        if text.startswith("```"):
            # Remove opening ``` (with optional language tag) and closing ```.
            lines: List[str] = text.split("\n")
            # Remove first line (```json or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines).strip()

        # Try to find JSON object boundaries.
        json_start: int = text.find("{")
        json_end: int = text.rfind("}")

        if json_start == -1 or json_end == -1 or json_end <= json_start:
            logger.warning("No JSON object found in LLM response: %s", raw_response[:200])
            return None

        json_str: str = text[json_start : json_end + 1]

        try:
            parsed: Dict[str, Any] = json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse JSON from LLM response: %s",
                json_str[:200],
            )
            return None

        # Validate required keys.
        required_keys: List[str] = ["severity", "confidence_score", "rationale"]
        for key in required_keys:
            if key not in parsed:
                logger.warning(
                    "LLM response JSON missing required key '%s': %s",
                    key,
                    json_str[:200],
                )
                return None

        return parsed

    def _check_ambiguous_candidates(
        self, candidates: List[Dict[str, Any]]
    ) -> bool:
        """Check if any two candidate severities have scores within 0.1.

        Compares all pairs of candidate confidence scores. If any two
        are within 0.1 of each other, the classification is considered
        ambiguous and should be escalated for human review.

        Args:
            candidates: List of candidate dicts, each with a
                ``confidence_score`` key.

        Returns:
            True if two or more candidates have scores within 0.1 of
            each other, False otherwise.
        """
        if len(candidates) < 2:
            return False

        scores: List[float] = []
        for c in candidates:
            try:
                scores.append(float(c.get("confidence_score", 0.0)))
            except (TypeError, ValueError):
                continue

        # Compare all pairs.
        for i in range(len(scores)):
            for j in range(i + 1, len(scores)):
                if abs(scores[i] - scores[j]) <= 0.1:
                    return True

        return False

    def _build_awaiting_review_result(
        self,
        parsed: Dict[str, Any],
        rationale_suffix: str = "",
    ) -> ClassificationResult:
        """Build a ClassificationResult with status ``awaiting_review``.

        Ensures ``candidate_severities`` contains at least two entries.
        If the parsed response has fewer than two candidates, synthetic
        entries are added.

        Args:
            parsed: The parsed LLM response dict containing ``severity``,
                ``confidence_score``, ``rationale``, and optionally
                ``all_candidates``.
            rationale_suffix: Optional text appended to the rationale to
                explain why the entry was escalated.

        Returns:
            A ClassificationResult with status ``awaiting_review`` and
            at least two candidate severities.
        """
        severity: str = parsed.get("severity", "low")
        if severity not in VALID_SEVERITIES:
            severity = "low"

        confidence: float = _clamp_confidence(parsed.get("confidence_score", 0.0))
        rationale: str = parsed.get("rationale", "Classification requires human review.")
        if rationale_suffix:
            rationale = f"{rationale} {rationale_suffix}"

        # Build candidate_severities from all_candidates or synthesise.
        candidates: List[Dict[str, Any]] = parsed.get("all_candidates", [])
        candidate_severities: List[Dict[str, Any]] = []

        for c in candidates:
            c_severity: str = c.get("severity", "")
            if c_severity in VALID_SEVERITIES:
                candidate_severities.append({
                    "severity": c_severity,
                    "confidence_score": _clamp_confidence(
                        c.get("confidence_score", 0.0)
                    ),
                    "rationale": c.get("rationale", ""),
                })

        # Ensure at least two candidates.
        if len(candidate_severities) < 1:
            candidate_severities.append({
                "severity": severity,
                "confidence_score": confidence,
                "rationale": rationale,
            })

        if len(candidate_severities) < 2:
            # Add a synthetic second candidate with a different severity.
            other_severities: List[str] = [
                s for s in VALID_SEVERITIES if s != severity
            ]
            fallback_severity: str = other_severities[0] if other_severities else "medium"
            candidate_severities.append({
                "severity": fallback_severity,
                "confidence_score": max(0.0, confidence - 0.1),
                "rationale": "Alternative severity considered during classification.",
            })

        return ClassificationResult(
            severity=severity,
            confidence_score=confidence,
            rationale=rationale,
            candidate_severities=candidate_severities,
            status="awaiting_review",
        )


    def classify(self, error: ExtractedError) -> ClassificationResult:
        """Classify an error entry using few-shot examples from DynamoDB.

        Retrieves severity examples, constructs a classification prompt,
        invokes the Bedrock model, and parses the response. Implements:

        - Low-confidence escalation: if confidence < threshold, status
          is set to ``awaiting_review`` with candidate_severities.
        - Ambiguous classification: if two severities are within 0.1,
          same escalation applies.
        - Unparseable response handling: retries once with a stricter
          prompt, then flags as ``awaiting_review``.
        - Critical error forwarding: if severity is ``critical`` and
          confidence >= threshold, invokes the notification callback.

        Args:
            error: The extracted error entry to classify.

        Returns:
            A ClassificationResult with the severity, confidence score,
            rationale, and status.
        """
        # Step 1: Fetch few-shot examples from DynamoDB.
        examples: List[Dict[str, str]] = self._fetch_severity_examples()

        # Step 2: Build prompt and invoke LLM.
        prompt: str = self._build_classification_prompt(
            error=error,
            examples=examples,
            strict=False,
        )
        raw_response: str = self._invoke_bedrock_model(prompt=prompt)

        # Step 3: Parse the LLM response.
        parsed: Optional[Dict[str, Any]] = self._parse_llm_response(
            raw_response=raw_response
        )

        # Step 4: Handle unparseable response — retry once with stricter prompt.
        if parsed is None:
            logger.warning(
                "LLM response was unparseable. Retrying with stricter prompt."
            )
            strict_prompt: str = self._build_classification_prompt(
                error=error,
                examples=examples,
                strict=True,
            )
            strict_response: str = self._invoke_bedrock_model(prompt=strict_prompt)
            parsed = self._parse_llm_response(raw_response=strict_response)

            if parsed is None:
                logger.error(
                    "LLM response still unparseable after retry. "
                    "Flagging as awaiting_review."
                )
                return self._build_awaiting_review_result(
                    parsed={
                        "severity": "medium",
                        "confidence_score": 0.0,
                        "rationale": "Unparseable LLM response after retry.",
                        "all_candidates": [],
                    },
                    rationale_suffix="(unparseable LLM response)",
                )

        # Step 5: Validate and clamp parsed values.
        severity: str = parsed.get("severity", "")
        if severity not in VALID_SEVERITIES:
            logger.warning(
                "LLM returned invalid severity '%s'. Flagging as awaiting_review.",
                severity,
            )
            return self._build_awaiting_review_result(
                parsed=parsed,
                rationale_suffix=f"(invalid severity: {severity})",
            )

        confidence: float = _clamp_confidence(parsed.get("confidence_score", 0.0))
        rationale: str = parsed.get("rationale", "")
        if not rationale or not rationale.strip():
            rationale = "No rationale provided by the model."

        candidates: List[Dict[str, Any]] = parsed.get("all_candidates", [])

        # Step 6: Check for low confidence or ambiguous classification.
        is_low_confidence: bool = confidence < self.confidence_threshold
        is_ambiguous: bool = self._check_ambiguous_candidates(
            candidates=candidates
        )

        if is_low_confidence or is_ambiguous:
            escalation_reason: str = ""
            if is_low_confidence:
                escalation_reason = (
                    f"Confidence {confidence:.2f} is below threshold "
                    f"{self.confidence_threshold:.2f}."
                )
            if is_ambiguous:
                ambiguity_note: str = (
                    "Two or more candidate severities have scores within 0.1."
                )
                escalation_reason = (
                    f"{escalation_reason} {ambiguity_note}".strip()
                )

            logger.info(
                "Classification escalated for human review: %s",
                escalation_reason,
            )

            return self._build_awaiting_review_result(
                parsed=parsed,
                rationale_suffix=f"(escalated: {escalation_reason})",
            )

        # Step 7: Build the final classified result.
        result: ClassificationResult = ClassificationResult(
            severity=severity,
            confidence_score=confidence,
            rationale=rationale,
            candidate_severities=None,
            status="classified",
        )

        # Step 8: Forward critical errors to the notification callback.
        if severity == "critical" and self._notification_callback is not None:
            logger.info(
                "Critical error classified with confidence %.2f. "
                "Forwarding to Notification_Agent.",
                confidence,
            )
            try:
                self._notification_callback(error, result)
            except Exception:
                logger.exception(
                    "Notification callback failed for critical error."
                )

        return result


def _clamp_confidence(value: Any) -> float:
    """Clamp a confidence score to the valid range [0.0, 1.0].

    Handles non-numeric values by returning 0.0.

    Args:
        value: The raw confidence score value from the LLM response.

    Returns:
        A float clamped to [0.0, 1.0].
    """
    try:
        score: float = float(value)
    except (TypeError, ValueError):
        return 0.0

    return max(0.0, min(1.0, score))
