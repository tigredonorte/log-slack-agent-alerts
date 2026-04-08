# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Admin API Lambda Handler for Severity Examples CRUD.

Provides REST API endpoints for managing severity classification examples
stored in DynamoDB. These examples are used as few-shot context by the
Classification_Agent to improve error severity classification accuracy.

Endpoints:
    POST   /examples              - Create a new severity example
    GET    /examples              - List all examples (optional ?severity= filter)
    PUT    /examples/{exampleId}  - Update an existing example
    DELETE /examples/{exampleId}  - Delete an existing example

Authentication is handled by a Cognito User Pools Authorizer attached to
the API Gateway. Unauthenticated requests never reach this Lambda — they
receive HTTP 401 from the authorizer.

Follows the FAST feedback API pattern:
    - aws_lambda_powertools for logging, tracing, and API Gateway resolution
    - APIGatewayRestResolver with CORS config
    - Pydantic models for request validation
    - boto3 DynamoDB low-level client with explicit attribute types

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

import os
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig
from aws_lambda_powertools.logging.correlation_paths import API_GATEWAY_REST
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Environment variables — fail loudly if missing (no fallback defaults)
# ---------------------------------------------------------------------------
TABLE_NAME: str = os.environ["TABLE_NAME"]
PREFIX: str = os.environ["PREFIX"]

# ---------------------------------------------------------------------------
# CORS configuration — mirrors the feedback API pattern
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS: str = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

# Parse CORS origins — can be a comma-separated list
cors_origins: List[str] = [
    origin.strip() for origin in CORS_ALLOWED_ORIGINS.split(",") if origin.strip()
]
primary_origin: str = cors_origins[0] if cors_origins else "*"
extra_origins: Optional[List[str]] = cors_origins[1:] if len(cors_origins) > 1 else None

cors_config: CORSConfig = CORSConfig(
    allow_origin=primary_origin,
    extra_origins=extra_origins,
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

# ---------------------------------------------------------------------------
# AWS clients and Powertools initialisation
# ---------------------------------------------------------------------------
dynamodb = boto3.client("dynamodb")

tracer: Tracer = Tracer()
logger: Logger = Logger()
app: APIGatewayRestResolver = APIGatewayRestResolver(cors=cors_config)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------
VALID_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
MIN_LOG_TEXT_LENGTH: int = 10

# GSI name used for severity-based queries
SEVERITY_GSI_NAME: str = "severity-createdAt-index"


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class CreateExampleRequest(BaseModel):
    """Request payload for creating a new severity example.

    Attributes:
        severity: One of low, medium, high, or critical.
        log_text: Sample log text demonstrating this severity (min 10 chars).
        description: Optional human description of why this log maps to the severity.
    """

    severity: Literal["low", "medium", "high", "critical"] = Field(
        ...,
        description="Severity level: low, medium, high, or critical",
    )
    log_text: str = Field(
        ...,
        min_length=MIN_LOG_TEXT_LENGTH,
        description=f"Sample log text (minimum {MIN_LOG_TEXT_LENGTH} characters)",
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional explanation of why this log text maps to the given severity",
    )


class UpdateExampleRequest(BaseModel):
    """Request payload for updating an existing severity example.

    All fields are optional — only provided fields are updated.

    Attributes:
        severity: One of low, medium, high, or critical.
        log_text: Sample log text demonstrating this severity (min 10 chars).
        description: Optional human description.
    """

    severity: Optional[Literal["low", "medium", "high", "critical"]] = Field(
        default=None,
        description="Severity level: low, medium, high, or critical",
    )
    log_text: Optional[str] = Field(
        default=None,
        min_length=MIN_LOG_TEXT_LENGTH,
        description=f"Sample log text (minimum {MIN_LOG_TEXT_LENGTH} characters)",
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional explanation of why this log text maps to the given severity",
    )


# ---------------------------------------------------------------------------
# Helper: convert a DynamoDB item dict to a plain JSON-serialisable dict
# ---------------------------------------------------------------------------
def _dynamo_item_to_dict(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a DynamoDB low-level item (with type descriptors) to a plain dict.

    Handles String (S) and Number (N) attribute types. Number values are
    returned as integers when they have no decimal component, otherwise as
    floats.

    Args:
        item: A DynamoDB item dict, e.g. {"exampleId": {"S": "abc-123"}, ...}.

    Returns:
        A plain dict with the type wrappers removed,
        e.g. {"exampleId": "abc-123", ...}.
    """
    result: Dict[str, Any] = {}
    for key, value in item.items():
        if "S" in value:
            result[key] = value["S"]
        elif "N" in value:
            # Preserve int vs float based on the stored string representation
            num_str: str = value["N"]
            result[key] = int(num_str) if "." not in num_str else float(num_str)
    return result


# ---------------------------------------------------------------------------
# POST /examples — create a new severity example
# ---------------------------------------------------------------------------
@app.post("/examples")
def create_example() -> tuple[Dict[str, Any], int]:
    """Create a new severity example in DynamoDB.

    Validates the request body using Pydantic, generates a UUID for the
    example, sets createdAt and updatedAt to the current epoch milliseconds,
    and stores the item in DynamoDB.

    Returns:
        A tuple of (response_body, http_status_code).
        201 on success, 400 on validation error, 500 on DynamoDB error.
    """
    try:
        # Parse and validate request body
        body: CreateExampleRequest = CreateExampleRequest(**app.current_event.json_body)

        # Generate identifiers and timestamps
        example_id: str = str(uuid.uuid4())
        now_ms: int = int(time.time() * 1000)

        # Build the DynamoDB item with explicit attribute types
        item: Dict[str, Any] = {
            "exampleId": {"S": example_id},
            "severity": {"S": body.severity},
            "logText": {"S": body.log_text},
            "createdAt": {"N": str(now_ms)},
            "updatedAt": {"N": str(now_ms)},
        }

        # Add optional description field if provided
        if body.description is not None:
            item["description"] = {"S": body.description}

        dynamodb.put_item(TableName=TABLE_NAME, Item=item)

        # Return the created example as a plain dict
        return _dynamo_item_to_dict(item), 201

    except ValueError as e:
        logger.warning(f"Validation error: {str(e)}")
        return {"error": str(e)}, 400

    except ClientError as e:
        logger.error(f"DynamoDB error: {e.response['Error']['Message']}")
        return {"error": "Internal server error"}, 500

    except Exception as e:
        logger.error(f"Error creating example: {str(e)}")
        return {"error": "Internal server error"}, 500


# ---------------------------------------------------------------------------
# GET /examples — list all examples, optional severity filter via GSI
# ---------------------------------------------------------------------------
@app.get("/examples")
def list_examples() -> tuple[Dict[str, Any], int]:
    """List severity examples from DynamoDB.

    If the ``severity`` query parameter is provided, queries the
    ``severity-createdAt-index`` GSI to return only matching examples.
    Otherwise, performs a full table scan to return all examples.

    Returns:
        A tuple of (response_body, http_status_code).
        200 on success, 400 if severity filter value is invalid,
        500 on DynamoDB error.
    """
    try:
        # Check for optional severity query parameter
        severity_filter: Optional[str] = app.current_event.get_query_string_value(
            name="severity",
            default_value=None,
        )

        if severity_filter is not None:
            # Validate the severity filter value
            if severity_filter not in VALID_SEVERITIES:
                return {
                    "error": (
                        f"Invalid severity filter '{severity_filter}'. "
                        f"Must be one of: {', '.join(VALID_SEVERITIES)}"
                    )
                }, 400

            # Query the GSI for examples matching the severity
            response = dynamodb.query(
                TableName=TABLE_NAME,
                IndexName=SEVERITY_GSI_NAME,
                KeyConditionExpression="severity = :sev",
                ExpressionAttributeValues={
                    ":sev": {"S": severity_filter},
                },
            )
        else:
            # No filter — scan the entire table
            response = dynamodb.scan(TableName=TABLE_NAME)

        # Convert each DynamoDB item to a plain dict
        examples: List[Dict[str, Any]] = [
            _dynamo_item_to_dict(item=item) for item in response.get("Items", [])
        ]

        return {"examples": examples}, 200

    except ClientError as e:
        logger.error(f"DynamoDB error: {e.response['Error']['Message']}")
        return {"error": "Internal server error"}, 500

    except Exception as e:
        logger.error(f"Error listing examples: {str(e)}")
        return {"error": "Internal server error"}, 500


# ---------------------------------------------------------------------------
# PUT /examples/<exampleId> — update an existing example
# ---------------------------------------------------------------------------
@app.put("/examples/<example_id>")
def update_example(example_id: str) -> tuple[Dict[str, Any], int]:
    """Update an existing severity example in DynamoDB.

    Checks that the example exists first (returns 404 if not found), then
    applies the provided updates. Sets ``updatedAt`` to the current epoch
    milliseconds.

    Args:
        example_id: The UUID of the example to update (from the URL path).

    Returns:
        A tuple of (response_body, http_status_code).
        200 on success, 400 on validation error, 404 if not found,
        500 on DynamoDB error.
    """
    try:
        # Parse and validate request body
        body: UpdateExampleRequest = UpdateExampleRequest(**app.current_event.json_body)

        # Check that the example exists
        existing = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"exampleId": {"S": example_id}},
        )

        if "Item" not in existing:
            return {"error": f"Example '{example_id}' not found"}, 404

        # Build update expression dynamically from provided fields
        now_ms: int = int(time.time() * 1000)

        # Always update updatedAt
        update_parts: List[str] = ["updatedAt = :updatedAt"]
        expression_values: Dict[str, Any] = {
            ":updatedAt": {"N": str(now_ms)},
        }

        if body.severity is not None:
            update_parts.append("severity = :severity")
            expression_values[":severity"] = {"S": body.severity}

        if body.log_text is not None:
            update_parts.append("logText = :logText")
            expression_values[":logText"] = {"S": body.log_text}

        if body.description is not None:
            update_parts.append("description = :description")
            expression_values[":description"] = {"S": body.description}

        update_expression: str = "SET " + ", ".join(update_parts)

        # Perform the update and return the new item
        result = dynamodb.update_item(
            TableName=TABLE_NAME,
            Key={"exampleId": {"S": example_id}},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
            ReturnValues="ALL_NEW",
        )

        return _dynamo_item_to_dict(item=result["Attributes"]), 200

    except ValueError as e:
        logger.warning(f"Validation error: {str(e)}")
        return {"error": str(e)}, 400

    except ClientError as e:
        logger.error(f"DynamoDB error: {e.response['Error']['Message']}")
        return {"error": "Internal server error"}, 500

    except Exception as e:
        logger.error(f"Error updating example: {str(e)}")
        return {"error": "Internal server error"}, 500


# ---------------------------------------------------------------------------
# DELETE /examples/<exampleId> — remove an existing example
# ---------------------------------------------------------------------------
@app.delete("/examples/<example_id>")
def delete_example(example_id: str) -> tuple[Dict[str, Any], int]:
    """Delete a severity example from DynamoDB.

    Checks that the example exists first (returns 404 if not found), then
    deletes it.

    Args:
        example_id: The UUID of the example to delete (from the URL path).

    Returns:
        A tuple of (response_body, http_status_code).
        200 on success, 404 if not found, 500 on DynamoDB error.
    """
    try:
        # Check that the example exists before deleting
        existing = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"exampleId": {"S": example_id}},
        )

        if "Item" not in existing:
            return {"error": f"Example '{example_id}' not found"}, 404

        # Delete the item
        dynamodb.delete_item(
            TableName=TABLE_NAME,
            Key={"exampleId": {"S": example_id}},
        )

        return {"success": True, "exampleId": example_id}, 200

    except ClientError as e:
        logger.error(f"DynamoDB error: {e.response['Error']['Message']}")
        return {"error": "Internal server error"}, 500

    except Exception as e:
        logger.error(f"Error deleting example: {str(e)}")
        return {"error": "Internal server error"}, 500


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------
@logger.inject_lambda_context(correlation_id_path=API_GATEWAY_REST)
def handler(event: dict, context: LambdaContext) -> dict:
    """Lambda handler for the Admin API.

    Resolves incoming API Gateway events to the appropriate route handler
    using aws_lambda_powertools APIGatewayRestResolver.

    Args:
        event: API Gateway REST event dict.
        context: Lambda execution context.

    Returns:
        API Gateway response dict with statusCode, headers, and body.
    """
    return app.resolve(event, context)
