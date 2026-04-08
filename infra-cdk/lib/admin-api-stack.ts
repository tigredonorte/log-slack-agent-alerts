import * as cdk from "aws-cdk-lib"
import * as apigateway from "aws-cdk-lib/aws-apigateway"
import * as cognito from "aws-cdk-lib/aws-cognito"
import * as dynamodb from "aws-cdk-lib/aws-dynamodb"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as logs from "aws-cdk-lib/aws-logs"
import * as ssm from "aws-cdk-lib/aws-ssm"
import { PythonFunction } from "@aws-cdk/aws-lambda-python-alpha"
import { Construct } from "constructs"
import * as path from "path"

/**
 * Properties for the AdminApiStack.
 *
 * @property prefix - Resource name prefix (e.g. "team5"). Used to namespace
 *   all resource names, SSM parameters, and environment variables.
 * @property userPoolId - The Cognito User Pool ID used for API authorization.
 *   Requests without a valid Cognito token receive HTTP 401.
 * @property severityExamplesTable - The DynamoDB Table construct for the
 *   Severity_Examples_Store. The Lambda is granted read/write access.
 */
export interface AdminApiStackProps extends cdk.NestedStackProps {
  /** Resource name prefix — must be provided by the caller. */
  readonly prefix: string
  /** Cognito User Pool ID for the API Gateway authorizer. */
  readonly userPoolId: string
  /** DynamoDB table for severity classification examples. */
  readonly severityExamplesTable: dynamodb.Table
}

/**
 * CDK nested stack that provisions the Admin API for managing severity
 * classification examples via REST endpoints.
 *
 * Resources created:
 *   - API Gateway REST API  `{prefix}-admin-api`
 *       Routes:
 *         POST   /examples              — Create a new severity example
 *         GET    /examples              — List examples (optional ?severity= filter)
 *         PUT    /examples/{exampleId}  — Update an existing example
 *         DELETE /examples/{exampleId}  — Delete an existing example
 *   - Lambda function  `{prefix}-admin-api`
 *       Handler: `infra-cdk/lambdas/admin_api/index.py`
 *       Environment: TABLE_NAME, PREFIX, CORS_ALLOWED_ORIGINS
 *   - Cognito User Pools Authorizer
 *       Protects all routes; unauthenticated requests receive HTTP 401
 *   - SSM parameter  `/{prefix}/admin-api-url`
 *       Stores the API Gateway URL for discovery by other components
 *
 * Follows the FAST template patterns:
 *   - Nested stack (same as BackendStack, CognitoStack, SeverityExamplesStack)
 *   - PythonFunction with Powertools ARM64 layer
 *   - Cognito User Pools Authorizer (same as feedback API)
 *   - SSM parameter for service discovery
 *
 * Requirements satisfied: 7.1, 7.2, 7.5
 */
export class AdminApiStack extends cdk.NestedStack {
  /** The API Gateway URL for the Admin API. */
  public readonly apiUrl: string

  constructor(scope: Construct, id: string, props: AdminApiStackProps) {
    super(scope, id, props)

    const { prefix, userPoolId, severityExamplesTable } = props

    // ── Lambda function ─────────────────────────────────────────────────
    const adminLambda = this.createAdminLambda(prefix, severityExamplesTable)

    // ── API Gateway + Cognito authorizer ────────────────────────────────
    this.apiUrl = this.createAdminApi(prefix, userPoolId, adminLambda)

    // ── SSM parameter ───────────────────────────────────────────────────
    this.storeSsmParameter(prefix)
  }

  /**
   * Creates the Admin API Lambda function using the PythonFunction construct.
   *
   * The Lambda reads severity examples from DynamoDB and is granted full
   * read/write access to the table. It uses the Powertools ARM64 layer for
   * structured logging, tracing, and API Gateway event resolution.
   *
   * Environment variables passed to the Lambda:
   *   - TABLE_NAME: Physical DynamoDB table name (from the table construct)
   *   - PREFIX: Resource name prefix (e.g. "team5")
   *   - CORS_ALLOWED_ORIGINS: Allowed CORS origins (wildcard for dev)
   *
   * @param prefix - Resource name prefix (e.g. "team5")
   * @param severityExamplesTable - The DynamoDB Table construct to grant access to
   * @returns The Lambda Function construct
   */
  private createAdminLambda(
    prefix: string,
    severityExamplesTable: dynamodb.Table
  ): lambda.Function {
    // ARM_64 required — matches Powertools ARM64 layer and avoids cross-platform issues
    const adminLambda = new PythonFunction(this, "AdminApiLambda", {
      functionName: `${prefix}-admin-api`,
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      // Points to infra-cdk/lambdas/admin_api/ which contains index.py + requirements.txt
      entry: path.join(__dirname, "..", "lambdas", "admin_api"), // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
      handler: "handler",
      environment: {
        TABLE_NAME: severityExamplesTable.tableName,
        PREFIX: prefix,
        CORS_ALLOWED_ORIGINS: "*",
      },
      timeout: cdk.Duration.seconds(30),
      layers: [
        lambda.LayerVersion.fromLayerVersionArn(
          this,
          "PowertoolsLayer",
          `arn:aws:lambda:${cdk.Stack.of(this).region}:017000801446:layer:AWSLambdaPowertoolsPythonV3-python313-arm64:18`
        ),
      ],
      logGroup: new logs.LogGroup(this, "AdminApiLambdaLogGroup", {
        logGroupName: `/aws/lambda/${prefix}-admin-api`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })

    // Grant Lambda full read/write access to the severity examples table
    severityExamplesTable.grantReadWriteData(adminLambda)

    return adminLambda
  }

  /**
   * Creates the API Gateway REST API with Cognito User Pools Authorizer and
   * wires all CRUD routes to the Admin Lambda.
   *
   * Routes:
   *   POST   /examples              → adminLambda (create example)
   *   GET    /examples              → adminLambda (list examples)
   *   PUT    /examples/{exampleId}  → adminLambda (update example)
   *   DELETE /examples/{exampleId}  → adminLambda (delete example)
   *
   * All routes require a valid Cognito JWT token in the Authorization header.
   * CORS preflight (OPTIONS) is handled automatically by API Gateway.
   *
   * @param prefix - Resource name prefix (e.g. "team5")
   * @param userPoolId - Cognito User Pool ID for the authorizer
   * @param adminLambda - The Lambda function to integrate with all routes
   * @returns The API Gateway URL string
   */
  private createAdminApi(
    prefix: string,
    userPoolId: string,
    adminLambda: lambda.Function
  ): string {
    // Import the Cognito User Pool from the provided ID
    const userPool = cognito.UserPool.fromUserPoolId(
      this,
      "ImportedUserPool",
      userPoolId
    )

    // Create the REST API with CORS and deployment options
    const api = new apigateway.RestApi(this, "AdminApi", {
      restApiName: `${prefix}-admin-api`,
      description:
        "Admin API for CRUD operations on severity classification examples",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allowHeaders: ["Content-Type", "Authorization"],
      },
      deployOptions: {
        stageName: "prod",
        throttlingRateLimit: 100,
        throttlingBurstLimit: 200,
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: true,
        metricsEnabled: true,
        accessLogDestination: new apigateway.LogGroupLogDestination(
          new logs.LogGroup(this, "AdminApiAccessLogGroup", {
            logGroupName: `/aws/apigateway/${prefix}-admin-api-access`,
            retention: logs.RetentionDays.ONE_WEEK,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
          })
        ),
        accessLogFormat: apigateway.AccessLogFormat.jsonWithStandardFields(),
        tracingEnabled: true,
      },
    })

    // Request validator for API security
    const requestValidator = new apigateway.RequestValidator(
      this,
      "AdminApiRequestValidator",
      {
        restApi: api,
        requestValidatorName: `${prefix}-admin-api-request-validator`,
        validateRequestBody: true,
        validateRequestParameters: true,
      }
    )

    // Cognito User Pools Authorizer — unauthenticated requests get HTTP 401
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(
      this,
      "AdminApiAuthorizer",
      {
        cognitoUserPools: [userPool],
        identitySource: "method.request.header.Authorization",
        authorizerName: `${prefix}-admin-api-authorizer`,
      }
    )

    // Shared method options for all authorized routes
    const authorizedMethodOptions: apigateway.MethodOptions = {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
      requestValidator,
    }

    // Lambda integration shared by all routes
    const lambdaIntegration = new apigateway.LambdaIntegration(adminLambda)

    // ── /examples resource ──────────────────────────────────────────────
    const examplesResource = api.root.addResource("examples")
    examplesResource.addMethod("POST", lambdaIntegration, authorizedMethodOptions)
    examplesResource.addMethod("GET", lambdaIntegration, authorizedMethodOptions)

    // ── /examples/{exampleId} resource ──────────────────────────────────
    const exampleIdResource = examplesResource.addResource("{exampleId}")
    exampleIdResource.addMethod("PUT", lambdaIntegration, authorizedMethodOptions)
    exampleIdResource.addMethod("DELETE", lambdaIntegration, authorizedMethodOptions)

    return api.url
  }

  /**
   * Stores the Admin API Gateway URL in SSM Parameter Store under the
   * `/{prefix}/` namespace so that other components (e.g. frontend, agents)
   * can discover it at runtime without hard-coding.
   *
   * @param prefix - Resource name prefix (e.g. "team5")
   */
  private storeSsmParameter(prefix: string): void {
    new ssm.StringParameter(this, "AdminApiUrlParam", {
      parameterName: `/${prefix}/admin-api-url`,
      stringValue: this.apiUrl,
      description:
        "Admin API Gateway URL for severity classification examples CRUD",
    })
  }
}
