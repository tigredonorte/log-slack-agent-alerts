# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property-based tests for Admin API CRUD round-trip (Property 5).

Feature: eks-log-slack-alerts, Property 5: Admin API CRUD round-trip

For any valid severity example (with ``log_text`` of at least 10 characters
and ``severity`` in {low, medium, high, critical}), creating it via POST,
then reading it via GET, then updating it via PUT, then reading again, then
deleting via DELETE, then reading again SHALL produce: the created example
on first read, the updated example on second read, and a not-found result
on third read.

Uses Hypothesis to generate random valid severity example payloads, then
exercises the full CRUD lifecycle against the Admin API Lambda handler
functions backed by an in-memory DynamoDB mock.

Validates: Requirements 4.1, 4.2, 4.3
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Ensure required environment variables are set BEFORE importing the
# admin_api module, which reads them at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("TABLE_NAME", "test-severity-examples")
os.environ.setdefault("PREFIX", "test")

# ---------------------------------------------------------------------------
# Import the admin_api module via sys.path + importlib, since the directory
# name "infra-cdk" contains a hyphen and cannot be used in dotted imports.
# ---------------------------------------------------------------------------
_ADMIN_API_DIR: Path = (
    Path(__file__).resolve().parents[2] / "infra-cdk" / "lambdas" / "admin_api"
)

if str(_ADMIN_API_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_API_DIR))

# Import the module (this triggers env var reads and boto3 client creation)
_admin_module = importlib.import_module("index")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_SEVERITIES: List[str] = ["low", "medium", "high", "critical"]
MIN_LOG_TEXT_LENGTH: int = 10


# ---------------------------------------------------------------------------
# In-memory DynamoDB mock
# ---------------------------------------------------------------------------
class InMemoryDynamoDB:
    """In-memory mock of the boto3 DynamoDB low-level client.

    Simulates put_item, get_item, scan, query, update_item, and
    delete_item operations using a plain Python dict as the backing
    store. Items are keyed by the ``exampleId`` string attribute.

    Attributes:
        store: Dict mapping exampleId strings to DynamoDB-formatted items.
    """

    def __init__(self) -> None:
        """Initialise an empty in-memory store."""
        self.store: Dict[str, Dict[str, Any]] = {}

    def put_item(self, **kwargs: Any) -> Dict[str, Any]:
        """Store an item, keyed by its exampleId attribute.

        Args:
            **kwargs: Must include ``Item`` with a DynamoDB-formatted dict
                containing an ``exampleId`` key with an ``S`` type descriptor.

        Returns:
            An empty dict (matching DynamoDB put_item response shape).
        """
        item: Dict[str, Any] = kwargs["Item"]
        example_id: str = item["exampleId"]["S"]
        self.store[example_id] = deepcopy(item)
        return {}

    def get_item(self, **kwargs: Any) -> Dict[str, Any]:
        """Retrieve an item by its exampleId.

        Args:
            **kwargs: Must include ``Key`` with ``exampleId`` → ``{"S": ...}``.

        Returns:
            A dict with ``Item`` if found, otherwise an empty dict.
        """
        key: str = kwargs["Key"]["exampleId"]["S"]
        if key in self.store:
            return {"Item": deepcopy(self.store[key])}
        return {}

    def scan(self, **kwargs: Any) -> Dict[str, Any]:
        """Return all items in the store.

        Args:
            **kwargs: Accepted but ignored (mirrors DynamoDB scan signature).

        Returns:
            A dict with ``Items`` containing all stored items.
        """
        return {"Items": [deepcopy(item) for item in self.store.values()]}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        """Query items by severity using the GSI simulation.

        Extracts the severity filter from ``ExpressionAttributeValues``
        and returns only items whose ``severity`` attribute matches.

        Args:
            **kwargs: Must include ``ExpressionAttributeValues`` with
                ``:sev`` → ``{"S": severity_value}``.

        Returns:
            A dict with ``Items`` containing matching items.
        """
        expression_values: Dict[str, Any] = kwargs.get(
            "ExpressionAttributeValues", {}
        )
        severity_filter: str = expression_values.get(":sev", {}).get("S", "")
        matching: List[Dict[str, Any]] = [
            deepcopy(item)
            for item in self.store.values()
            if item.get("severity", {}).get("S") == severity_filter
        ]
        return {"Items": matching}

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        """Update an existing item's attributes.

        Parses the ``UpdateExpression`` to determine which fields to set,
        then applies the values from ``ExpressionAttributeValues``.

        Args:
            **kwargs: Must include ``Key``, ``UpdateExpression``,
                ``ExpressionAttributeValues``, and ``ReturnValues``.

        Returns:
            A dict with ``Attributes`` containing the updated item.

        Raises:
            KeyError: If the item does not exist in the store.
        """
        key: str = kwargs["Key"]["exampleId"]["S"]
        if key not in self.store:
            raise KeyError(f"Item {key} not found")

        expression_values: Dict[str, Any] = kwargs.get(
            "ExpressionAttributeValues", {}
        )

        # Apply each expression attribute value to the stored item.
        # The UpdateExpression format is "SET field1 = :field1, field2 = :field2"
        update_expr: str = kwargs.get("UpdateExpression", "")
        # Extract "fieldName = :placeholder" pairs from the SET clause
        set_clause: str = update_expr.replace("SET ", "")
        assignments: List[str] = [a.strip() for a in set_clause.split(",")]

        for assignment in assignments:
            # Each assignment looks like "fieldName = :placeholder"
            parts = assignment.split("=")
            if len(parts) == 2:
                field_name: str = parts[0].strip()
                placeholder: str = parts[1].strip()
                if placeholder in expression_values:
                    self.store[key][field_name] = deepcopy(
                        expression_values[placeholder]
                    )

        return {"Attributes": deepcopy(self.store[key])}

    def delete_item(self, **kwargs: Any) -> Dict[str, Any]:
        """Remove an item by its exampleId.

        Args:
            **kwargs: Must include ``Key`` with ``exampleId`` → ``{"S": ...}``.

        Returns:
            An empty dict (matching DynamoDB delete_item response shape).
        """
        key: str = kwargs["Key"]["exampleId"]["S"]
        self.store.pop(key, None)
        return {}


# ---------------------------------------------------------------------------
# Helper: build a mock API Gateway event for the Lambda handler
# ---------------------------------------------------------------------------
def _build_api_gateway_event(
    http_method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    query_params: Optional[Dict[str, str]] = None,
    path_params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build a minimal API Gateway REST event dict.

    Constructs an event dict that the aws_lambda_powertools
    APIGatewayRestResolver can parse and route to the correct handler.

    Args:
        http_method: HTTP method (GET, POST, PUT, DELETE).
        path: Request path (e.g. "/examples" or "/examples/{exampleId}").
        body: Optional request body dict (will be JSON-serialised).
        query_params: Optional query string parameters dict.
        path_params: Optional path parameters dict.

    Returns:
        A dict matching the API Gateway REST event structure.
    """
    # Determine the resource path template for routing
    resource: str = path
    if path_params and "example_id" in path_params:
        resource = "/examples/{example_id}"

    event: Dict[str, Any] = {
        "httpMethod": http_method,
        "path": path,
        "resource": resource,
        "headers": {
            "Content-Type": "application/json",
        },
        "queryStringParameters": query_params,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
        "requestContext": {
            "stage": "test",
            "requestId": "test-request-id",
            "identity": {},
            "resourcePath": resource,
            "httpMethod": http_method,
            "accountId": "123456789012",
        },
        "stageVariables": None,
        "multiValueHeaders": {},
        "multiValueQueryStringParameters": None,
    }
    return event


def _parse_handler_response(response: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
    """Parse the API Gateway response from the Lambda handler.

    Extracts the status code and deserialises the JSON body.

    Args:
        response: The dict returned by the Lambda handler, containing
            ``statusCode`` and ``body`` keys.

    Returns:
        A tuple of (parsed_body_dict, http_status_code).
    """
    status_code: int = response["statusCode"]
    body: Dict[str, Any] = json.loads(response.get("body", "{}"))
    return body, status_code


# ---------------------------------------------------------------------------
# Hypothesis strategies for valid severity examples
# ---------------------------------------------------------------------------

# Strategy for generating a valid severity level.
severity_strategy: st.SearchStrategy[str] = st.sampled_from(VALID_SEVERITIES)

# Strategy for generating valid log text (min 10 chars, max 200 chars).
# Excludes control characters that could break JSON serialisation.
log_text_strategy: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="\x00\r\n",
    ),
    min_size=MIN_LOG_TEXT_LENGTH,
    max_size=200,
).filter(lambda s: len(s.strip()) >= MIN_LOG_TEXT_LENGTH)

# Strategy for generating an optional description (None or non-empty text).
description_strategy: st.SearchStrategy[Optional[str]] = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "P", "Z"),
            blacklist_characters="\x00\r\n",
        ),
        min_size=1,
        max_size=100,
    ).filter(lambda s: s.strip()),
)

# Strategy for generating updated log text (different from original).
updated_log_text_strategy: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="\x00\r\n",
    ),
    min_size=MIN_LOG_TEXT_LENGTH,
    max_size=200,
).filter(lambda s: len(s.strip()) >= MIN_LOG_TEXT_LENGTH)


# ---------------------------------------------------------------------------
# Property 5: Admin API CRUD round-trip
# ---------------------------------------------------------------------------


class TestAdminApiCrudRoundTrip:
    """Property 5: Admin API CRUD round-trip.

    For any valid severity example, create → read → update → read →
    delete → read SHALL produce expected results at each step.

    **Validates: Requirements 4.1, 4.2, 4.3**
    """

    @given(
        severity=severity_strategy,
        log_text=log_text_strategy,
        description=description_strategy,
        updated_log_text=updated_log_text_strategy,
    )
    @settings(max_examples=100)
    def test_crud_round_trip_produces_expected_results_at_each_step(
        self,
        severity: str,
        log_text: str,
        description: Optional[str],
        updated_log_text: str,
    ) -> None:
        """For any valid severity example payload, the full CRUD lifecycle
        SHALL produce the correct result at each step:

        1. POST /examples → 201, returned item has all fields
        2. GET /examples → created item appears in the list
        3. PUT /examples/{id} with updated fields → 200, fields updated
        4. GET /examples → updated item appears with new values
        5. DELETE /examples/{id} → 200
        6. GET /examples → item no longer in the list

        Args:
            severity: Random valid severity level.
            log_text: Random valid log text (>= 10 chars).
            description: Random optional description or None.
            updated_log_text: Random valid log text for the update step.
        """
        # Pick an updated severity that differs from the original
        updated_severity: str = [s for s in VALID_SEVERITIES if s != severity][0]

        # Create a fresh in-memory DynamoDB mock for this test iteration
        mock_dynamo: InMemoryDynamoDB = InMemoryDynamoDB()

        # Swap the module-level dynamodb client with our in-memory mock
        original_dynamodb = _admin_module.dynamodb
        _admin_module.dynamodb = mock_dynamo

        try:
            handler = _admin_module.handler
            mock_context: MagicMock = MagicMock()

            # -----------------------------------------------------------
            # Step 1: POST /examples → create a new severity example
            # -----------------------------------------------------------
            create_body: Dict[str, Any] = {
                "severity": severity,
                "log_text": log_text,
            }
            if description is not None:
                create_body["description"] = description

            create_event: Dict[str, Any] = _build_api_gateway_event(
                http_method="POST",
                path="/examples",
                body=create_body,
            )

            create_response: Dict[str, Any] = handler(
                create_event, mock_context
            )
            create_result, create_status = _parse_handler_response(
                response=create_response
            )

            # Verify: 201 status and all expected fields present
            assert create_status == 201, (
                f"POST /examples should return 201, got {create_status}. "
                f"Body: {create_result}"
            )
            assert "exampleId" in create_result, (
                "Created example must have an exampleId field"
            )
            assert create_result["severity"] == severity, (
                f"Created severity should be '{severity}', "
                f"got '{create_result.get('severity')}'"
            )
            assert create_result["logText"] == log_text, (
                "Created logText should match input"
            )
            assert "createdAt" in create_result, (
                "Created example must have a createdAt field"
            )
            assert "updatedAt" in create_result, (
                "Created example must have an updatedAt field"
            )

            # If description was provided, it should be in the response
            if description is not None:
                assert create_result.get("description") == description, (
                    "Created description should match input"
                )

            example_id: str = create_result["exampleId"]

            # -----------------------------------------------------------
            # Step 2: GET /examples → created item appears in list
            # -----------------------------------------------------------
            list_event_1: Dict[str, Any] = _build_api_gateway_event(
                http_method="GET",
                path="/examples",
            )

            list_response_1: Dict[str, Any] = handler(
                list_event_1, mock_context
            )
            list_result_1, list_status_1 = _parse_handler_response(
                response=list_response_1
            )

            assert list_status_1 == 200, (
                f"GET /examples should return 200, got {list_status_1}"
            )
            assert "examples" in list_result_1, (
                "GET /examples response must contain 'examples' key"
            )

            # Find the created item in the list
            found_items_1: List[Dict[str, Any]] = [
                ex
                for ex in list_result_1["examples"]
                if ex.get("exampleId") == example_id
            ]
            assert len(found_items_1) == 1, (
                f"Created example {example_id} should appear exactly "
                f"once in GET /examples, found {len(found_items_1)}"
            )
            assert found_items_1[0]["severity"] == severity, (
                f"Listed item severity should be '{severity}'"
            )
            assert found_items_1[0]["logText"] == log_text, (
                "Listed item logText should match created value"
            )

            # -----------------------------------------------------------
            # Step 3: PUT /examples/{id} → update fields
            # -----------------------------------------------------------
            update_body: Dict[str, Any] = {
                "severity": updated_severity,
                "log_text": updated_log_text,
            }

            update_event: Dict[str, Any] = _build_api_gateway_event(
                http_method="PUT",
                path=f"/examples/{example_id}",
                body=update_body,
                path_params={"example_id": example_id},
            )

            update_response: Dict[str, Any] = handler(
                update_event, mock_context
            )
            update_result, update_status = _parse_handler_response(
                response=update_response
            )

            assert update_status == 200, (
                f"PUT /examples/{example_id} should return 200, "
                f"got {update_status}. Body: {update_result}"
            )
            assert update_result["severity"] == updated_severity, (
                f"Updated severity should be '{updated_severity}', "
                f"got '{update_result.get('severity')}'"
            )
            assert update_result["logText"] == updated_log_text, (
                "Updated logText should match the new value"
            )

            # -----------------------------------------------------------
            # Step 4: GET /examples → updated item appears with new values
            # -----------------------------------------------------------
            list_event_2: Dict[str, Any] = _build_api_gateway_event(
                http_method="GET",
                path="/examples",
            )

            list_response_2: Dict[str, Any] = handler(
                list_event_2, mock_context
            )
            list_result_2, list_status_2 = _parse_handler_response(
                response=list_response_2
            )

            assert list_status_2 == 200, (
                f"GET /examples should return 200, got {list_status_2}"
            )

            found_items_2: List[Dict[str, Any]] = [
                ex
                for ex in list_result_2["examples"]
                if ex.get("exampleId") == example_id
            ]
            assert len(found_items_2) == 1, (
                f"Updated example {example_id} should appear exactly "
                f"once in GET /examples, found {len(found_items_2)}"
            )
            assert found_items_2[0]["severity"] == updated_severity, (
                f"Listed item severity should be '{updated_severity}' "
                f"after update"
            )
            assert found_items_2[0]["logText"] == updated_log_text, (
                "Listed item logText should match updated value"
            )

            # -----------------------------------------------------------
            # Step 5: DELETE /examples/{id} → remove the example
            # -----------------------------------------------------------
            delete_event: Dict[str, Any] = _build_api_gateway_event(
                http_method="DELETE",
                path=f"/examples/{example_id}",
                path_params={"example_id": example_id},
            )

            delete_response: Dict[str, Any] = handler(
                delete_event, mock_context
            )
            delete_result, delete_status = _parse_handler_response(
                response=delete_response
            )

            assert delete_status == 200, (
                f"DELETE /examples/{example_id} should return 200, "
                f"got {delete_status}. Body: {delete_result}"
            )

            # -----------------------------------------------------------
            # Step 6: GET /examples → item no longer in the list
            # -----------------------------------------------------------
            list_event_3: Dict[str, Any] = _build_api_gateway_event(
                http_method="GET",
                path="/examples",
            )

            list_response_3: Dict[str, Any] = handler(
                list_event_3, mock_context
            )
            list_result_3, list_status_3 = _parse_handler_response(
                response=list_response_3
            )

            assert list_status_3 == 200, (
                f"GET /examples should return 200, got {list_status_3}"
            )

            found_items_3: List[Dict[str, Any]] = [
                ex
                for ex in list_result_3["examples"]
                if ex.get("exampleId") == example_id
            ]
            assert len(found_items_3) == 0, (
                f"Deleted example {example_id} should NOT appear in "
                f"GET /examples after deletion, but found "
                f"{len(found_items_3)} items"
            )

        finally:
            # Restore the original dynamodb client on the module
            _admin_module.dynamodb = original_dynamodb


# ---------------------------------------------------------------------------
# Property 6: Admin API severity filter correctness
# ---------------------------------------------------------------------------


class TestAdminApiSeverityFilterCorrectness:
    """Property 6: Admin API severity filter correctness.

    For any set of severity examples in the store and any severity filter
    value, a GET request with the ``severity`` query parameter SHALL return
    only examples whose severity matches the filter value, and SHALL return
    all such matching examples.

    **Validates: Requirements 4.4**
    """

    @given(
        examples=st.lists(
            st.tuples(severity_strategy, log_text_strategy, description_strategy),
            min_size=2,
            max_size=10,
        ),
        filter_severity=severity_strategy,
    )
    @settings(max_examples=100)
    def test_severity_filter_returns_only_and_all_matching_examples(
        self,
        examples: list[tuple[str, str, Optional[str]]],
        filter_severity: str,
    ) -> None:
        """For any set of examples and any severity filter, GET /examples
        with the ``severity`` query parameter SHALL return only examples
        whose severity matches the filter, and SHALL return all such
        matching examples (no extras, no missing).

        Steps:
            1. Create multiple examples via POST /examples with different
               severities.
            2. GET /examples?severity={filter_severity} → collect results.
            3. Verify every returned example has the filtered severity.
            4. Verify the count of returned examples equals the count of
               created examples that have the filtered severity.

        Args:
            examples: A list of 2-10 tuples of (severity, log_text,
                description) representing example payloads to create.
            filter_severity: The severity value to filter by in the GET
                request.
        """
        # Create a fresh in-memory DynamoDB mock for this test iteration
        mock_dynamo: InMemoryDynamoDB = InMemoryDynamoDB()

        # Swap the module-level dynamodb client with our in-memory mock
        original_dynamodb = _admin_module.dynamodb
        _admin_module.dynamodb = mock_dynamo

        try:
            handler = _admin_module.handler
            mock_context: MagicMock = MagicMock()

            # Track which example IDs were created with which severity
            created_ids_by_severity: Dict[str, List[str]] = {
                sev: [] for sev in VALID_SEVERITIES
            }

            # -----------------------------------------------------------
            # Step 1: Create all examples via POST /examples
            # -----------------------------------------------------------
            for severity, log_text, description in examples:
                create_body: Dict[str, Any] = {
                    "severity": severity,
                    "log_text": log_text,
                }
                if description is not None:
                    create_body["description"] = description

                create_event: Dict[str, Any] = _build_api_gateway_event(
                    http_method="POST",
                    path="/examples",
                    body=create_body,
                )

                create_response: Dict[str, Any] = handler(
                    create_event, mock_context
                )
                create_result, create_status = _parse_handler_response(
                    response=create_response
                )

                assert create_status == 201, (
                    f"POST /examples should return 201, got {create_status}. "
                    f"Body: {create_result}"
                )

                example_id: str = create_result["exampleId"]
                created_ids_by_severity[severity].append(example_id)

            # -----------------------------------------------------------
            # Step 2: GET /examples?severity={filter_severity}
            # -----------------------------------------------------------
            filter_event: Dict[str, Any] = _build_api_gateway_event(
                http_method="GET",
                path="/examples",
                query_params={"severity": filter_severity},
            )

            filter_response: Dict[str, Any] = handler(
                filter_event, mock_context
            )
            filter_result, filter_status = _parse_handler_response(
                response=filter_response
            )

            assert filter_status == 200, (
                f"GET /examples?severity={filter_severity} should return "
                f"200, got {filter_status}. Body: {filter_result}"
            )
            assert "examples" in filter_result, (
                "GET /examples response must contain 'examples' key"
            )

            returned_examples: List[Dict[str, Any]] = filter_result["examples"]

            # -----------------------------------------------------------
            # Step 3: Verify every returned example has the filtered
            #         severity (no non-matching examples)
            # -----------------------------------------------------------
            for returned_ex in returned_examples:
                assert returned_ex["severity"] == filter_severity, (
                    f"Filtered GET returned an example with severity "
                    f"'{returned_ex['severity']}' but filter was "
                    f"'{filter_severity}'. Example ID: "
                    f"{returned_ex.get('exampleId')}"
                )

            # -----------------------------------------------------------
            # Step 4: Verify ALL matching examples are returned
            #         (no missing ones)
            # -----------------------------------------------------------
            expected_ids: List[str] = created_ids_by_severity[filter_severity]
            returned_ids: List[str] = [
                ex["exampleId"] for ex in returned_examples
            ]

            # Same count
            assert len(returned_ids) == len(expected_ids), (
                f"Expected {len(expected_ids)} examples with severity "
                f"'{filter_severity}', but GET returned "
                f"{len(returned_ids)}. Expected IDs: {expected_ids}, "
                f"Returned IDs: {returned_ids}"
            )

            # Same set of IDs (order may differ)
            assert set(returned_ids) == set(expected_ids), (
                f"Returned example IDs do not match expected IDs for "
                f"severity '{filter_severity}'. "
                f"Missing: {set(expected_ids) - set(returned_ids)}, "
                f"Extra: {set(returned_ids) - set(expected_ids)}"
            )

        finally:
            # Restore the original dynamodb client on the module
            _admin_module.dynamodb = original_dynamodb


# ---------------------------------------------------------------------------
# Property 7: Admin API input validation
# ---------------------------------------------------------------------------


class TestAdminApiInputValidation:
    """Property 7: Admin API input validation.

    For any example payload where ``log_text`` has fewer than 10 characters
    OR ``severity`` is not one of {low, medium, high, critical}, the
    Admin_API SHALL reject the request with an HTTP 400 status code.

    Feature: eks-log-slack-alerts, Property 7: Admin API input validation

    **Validates: Requirements 4.5**
    """

    # Strategy for generating short log text (0-9 characters).
    # These should always be rejected by the API's 10-char minimum.
    _short_log_text_strategy: st.SearchStrategy[str] = st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "P", "Z"),
            blacklist_characters="\x00\r\n",
        ),
        min_size=0,
        max_size=9,
    )

    # Strategy for generating invalid severity values — strings that are
    # NOT one of the four valid severity levels.
    _invalid_severity_strategy: st.SearchStrategy[str] = st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "P", "Z"),
            blacklist_characters="\x00\r\n",
        ),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s.strip().lower() not in {"low", "medium", "high", "critical"})

    @given(
        short_log_text=_short_log_text_strategy,
        valid_severity=severity_strategy,
    )
    @settings(max_examples=100)
    def test_short_log_text_rejected_with_400(
        self,
        short_log_text: str,
        valid_severity: str,
    ) -> None:
        """For any log_text with fewer than 10 characters and a valid
        severity, POST /examples SHALL return HTTP 400.

        This verifies that the Admin API enforces the minimum log_text
        length constraint regardless of the severity value.

        Args:
            short_log_text: Random text with 0-9 characters.
            valid_severity: A valid severity from {low, medium, high, critical}.
        """
        # Create a fresh in-memory DynamoDB mock for this test iteration
        mock_dynamo: InMemoryDynamoDB = InMemoryDynamoDB()

        # Swap the module-level dynamodb client with our in-memory mock
        original_dynamodb = _admin_module.dynamodb
        _admin_module.dynamodb = mock_dynamo

        try:
            handler = _admin_module.handler
            mock_context: MagicMock = MagicMock()

            # Build a payload with short log_text but valid severity
            create_body: Dict[str, Any] = {
                "severity": valid_severity,
                "log_text": short_log_text,
            }

            create_event: Dict[str, Any] = _build_api_gateway_event(
                http_method="POST",
                path="/examples",
                body=create_body,
            )

            response: Dict[str, Any] = handler(create_event, mock_context)
            _, status_code = _parse_handler_response(response=response)

            assert status_code == 400, (
                f"POST /examples with log_text of {len(short_log_text)} chars "
                f"(< 10) should return 400, got {status_code}. "
                f"log_text={short_log_text!r}, severity={valid_severity!r}"
            )

        finally:
            # Restore the original dynamodb client on the module
            _admin_module.dynamodb = original_dynamodb

    @given(
        valid_log_text=log_text_strategy,
        invalid_severity=_invalid_severity_strategy,
    )
    @settings(max_examples=100)
    def test_invalid_severity_rejected_with_400(
        self,
        valid_log_text: str,
        invalid_severity: str,
    ) -> None:
        """For any log_text with >= 10 characters and a severity NOT in
        {low, medium, high, critical}, POST /examples SHALL return HTTP 400.

        This verifies that the Admin API enforces the severity enum
        constraint regardless of the log_text value.

        Args:
            valid_log_text: Random text with >= 10 characters.
            invalid_severity: A string that is not a valid severity level.
        """
        # Create a fresh in-memory DynamoDB mock for this test iteration
        mock_dynamo: InMemoryDynamoDB = InMemoryDynamoDB()

        # Swap the module-level dynamodb client with our in-memory mock
        original_dynamodb = _admin_module.dynamodb
        _admin_module.dynamodb = mock_dynamo

        try:
            handler = _admin_module.handler
            mock_context: MagicMock = MagicMock()

            # Build a payload with valid log_text but invalid severity
            create_body: Dict[str, Any] = {
                "severity": invalid_severity,
                "log_text": valid_log_text,
            }

            create_event: Dict[str, Any] = _build_api_gateway_event(
                http_method="POST",
                path="/examples",
                body=create_body,
            )

            response: Dict[str, Any] = handler(create_event, mock_context)
            _, status_code = _parse_handler_response(response=response)

            assert status_code == 400, (
                f"POST /examples with invalid severity {invalid_severity!r} "
                f"should return 400, got {status_code}. "
                f"log_text={valid_log_text!r}"
            )

        finally:
            # Restore the original dynamodb client on the module
            _admin_module.dynamodb = original_dynamodb
