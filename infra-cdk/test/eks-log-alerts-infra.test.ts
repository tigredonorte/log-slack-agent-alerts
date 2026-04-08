/**
 * CDK infrastructure tests for the EKS Log Alerts feature.
 *
 * Validates that all required AWS resources are created with correct naming,
 * SSM parameters are stored under the /{PREFIX}/ namespace, configuration
 * accepts all required parameters, and environment variables are passed to
 * Runtime and Lambda functions.
 *
 * Validates: Requirements 7.1, 7.2, 7.3, 7.4, 9.3
 */
import * as cdk from "aws-cdk-lib"
import { Template, Match } from "aws-cdk-lib/assertions"
import * as dynamodb from "aws-cdk-lib/aws-dynamodb"
import * as iam from "aws-cdk-lib/aws-iam"
import { SeverityExamplesStack } from "../lib/severity-examples-stack"
import { AdminApiStack } from "../lib/admin-api-stack"
import { SlackWebhookStack } from "../lib/slack-webhook-stack"
import { ConfigManager, AppConfig } from "../lib/utils/config-manager"
import * as path from "path"

/** Test prefix used across all test cases. */
const TEST_PREFIX = "team5"

/**
 * Creates a parent CDK stack for hosting nested stacks in tests.
 *
 * @returns A tuple of [cdk.App, cdk.Stack] for use as the scope of nested stacks.
 */
function createTestParentStack(): [cdk.App, cdk.Stack] {
  const app = new cdk.App()
  const parentStack = new cdk.Stack(app, "TestParentStack", {
    env: { account: "123456789012", region: "us-east-1" },
  })
  return [app, parentStack]
}

/**
 * Creates a mock DynamoDB table inside the given stack for use as a
 * dependency in nested stack tests.
 *
 * @param stack - The parent CDK stack to create the table in.
 * @param prefix - Resource name prefix (e.g. "team5").
 * @returns The DynamoDB Table construct.
 */
function createMockDynamoTable(stack: cdk.Stack, prefix: string): dynamodb.Table {
  return new dynamodb.Table(stack, "MockSeverityExamplesTable", {
    tableName: `${prefix}-severity-examples`,
    partitionKey: { name: "exampleId", type: dynamodb.AttributeType.STRING },
    removalPolicy: cdk.RemovalPolicy.DESTROY,
  })
}

// ─────────────────────────────────────────────────────────────────────────────
// 1. CDK synth snapshot includes all required resources
//    Validates: Requirement 7.1
// ─────────────────────────────────────────────────────────────────────────────

describe("CDK synth includes all required resources (Req 7.1)", () => {
  /**
   * Verifies that the SeverityExamplesStack creates a DynamoDB table
   * with the correct partition key and a GSI named severity-createdAt-index.
   */
  test("SeverityExamplesStack creates DynamoDB table with GSI", () => {
    const [_app, parentStack] = createTestParentStack()

    new SeverityExamplesStack(parentStack, "SeverityExamples", {
      prefix: TEST_PREFIX,
    })

    const template = Template.fromStack(parentStack)

    // DynamoDB table exists with correct partition key
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: `${TEST_PREFIX}-severity-examples`,
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: "exampleId", KeyType: "HASH" }),
      ]),
    })

    // GSI severity-createdAt-index exists
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "severity-createdAt-index",
          KeySchema: Match.arrayWith([
            Match.objectLike({ AttributeName: "severity", KeyType: "HASH" }),
            Match.objectLike({ AttributeName: "createdAt", KeyType: "RANGE" }),
          ]),
        }),
      ]),
    })
  })

  /**
   * Verifies that the SeverityExamplesStack creates an SSM parameter
   * for the table name.
   */
  test("SeverityExamplesStack creates SSM parameter for table name", () => {
    const [_app, parentStack] = createTestParentStack()

    new SeverityExamplesStack(parentStack, "SeverityExamples", {
      prefix: TEST_PREFIX,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${TEST_PREFIX}/severity-examples-table-name`,
    })
  })

  /**
   * Verifies that the AdminApiStack creates an API Gateway REST API,
   * a Lambda function, and a Cognito authorizer.
   */
  test("AdminApiStack creates API Gateway, Lambda, and Cognito authorizer", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockTable = createMockDynamoTable(parentStack, TEST_PREFIX)

    new AdminApiStack(parentStack, "AdminApi", {
      prefix: TEST_PREFIX,
      userPoolId: "us-east-1_TestPoolId",
      severityExamplesTable: mockTable,
    })

    const template = Template.fromStack(parentStack)

    // API Gateway REST API exists
    template.hasResourceProperties("AWS::ApiGateway::RestApi", {
      Name: `${TEST_PREFIX}-admin-api`,
    })

    // Cognito authorizer exists
    template.hasResourceProperties("AWS::ApiGateway::Authorizer", {
      Type: "COGNITO_USER_POOLS",
    })

    // Lambda function exists
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${TEST_PREFIX}-admin-api`,
    })

    // SSM parameter for API URL exists
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${TEST_PREFIX}/admin-api-url`,
    })
  })

  /**
   * Verifies that the SlackWebhookStack creates a Lambda function and
   * an AgentCore Gateway target.
   */
  test("SlackWebhookStack creates Lambda and Gateway target", () => {
    const [_app, parentStack] = createTestParentStack()

    // Create a mock IAM role for the gateway
    const mockGatewayRole = new iam.Role(parentStack, "MockGatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    })

    new SlackWebhookStack(parentStack, "SlackWebhook", {
      prefix: TEST_PREFIX,
      slackChannelWebhookUrl: "https://hooks.slack.com/services/T00/B00/xxx",
      gatewayId: "test-gateway-id",
      gatewayRole: mockGatewayRole,
    })

    const template = Template.fromStack(parentStack)

    // Lambda function exists
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${TEST_PREFIX}-slack-webhook-tool`,
    })

    // AgentCore Gateway target exists
    template.hasResourceProperties("AWS::BedrockAgentCore::GatewayTarget", {
      GatewayIdentifier: "test-gateway-id",
      Name: "slack-webhook-target",
    })
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// 2. All resource names contain PREFIX
//    Validates: Requirement 7.2
// ─────────────────────────────────────────────────────────────────────────────

describe("All resource names contain PREFIX (Req 7.2)", () => {
  /**
   * Verifies that the DynamoDB table name is prefixed with the PREFIX value.
   */
  test("DynamoDB table name contains PREFIX", () => {
    const [_app, parentStack] = createTestParentStack()

    new SeverityExamplesStack(parentStack, "SeverityExamples", {
      prefix: TEST_PREFIX,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp(`^${TEST_PREFIX}-`),
    })
  })

  /**
   * Verifies that the Admin API Lambda function name is prefixed.
   */
  test("Admin API Lambda function name contains PREFIX", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockTable = createMockDynamoTable(parentStack, TEST_PREFIX)

    new AdminApiStack(parentStack, "AdminApi", {
      prefix: TEST_PREFIX,
      userPoolId: "us-east-1_TestPoolId",
      severityExamplesTable: mockTable,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp(`^${TEST_PREFIX}-`),
    })
  })

  /**
   * Verifies that the Admin API Gateway REST API name is prefixed.
   */
  test("Admin API Gateway name contains PREFIX", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockTable = createMockDynamoTable(parentStack, TEST_PREFIX)

    new AdminApiStack(parentStack, "AdminApi", {
      prefix: TEST_PREFIX,
      userPoolId: "us-east-1_TestPoolId",
      severityExamplesTable: mockTable,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::ApiGateway::RestApi", {
      Name: Match.stringLikeRegexp(`^${TEST_PREFIX}-`),
    })
  })

  /**
   * Verifies that the Slack webhook Lambda function name is prefixed.
   */
  test("Slack webhook Lambda function name contains PREFIX", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockGatewayRole = new iam.Role(parentStack, "MockGatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    })

    new SlackWebhookStack(parentStack, "SlackWebhook", {
      prefix: TEST_PREFIX,
      slackChannelWebhookUrl: "https://hooks.slack.com/services/T00/B00/xxx",
      gatewayId: "test-gateway-id",
      gatewayRole: mockGatewayRole,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp(`^${TEST_PREFIX}-`),
    })
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// 3. SSM parameters stored under /{PREFIX}/ namespace
//    Validates: Requirement 7.3
// ─────────────────────────────────────────────────────────────────────────────

describe("SSM parameters stored under /{PREFIX}/ namespace (Req 7.3)", () => {
  /**
   * Verifies that the severity examples table name SSM parameter is stored
   * under the /{PREFIX}/ namespace.
   */
  test("Severity examples table name SSM parameter uses /{PREFIX}/ namespace", () => {
    const [_app, parentStack] = createTestParentStack()

    new SeverityExamplesStack(parentStack, "SeverityExamples", {
      prefix: TEST_PREFIX,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: Match.stringLikeRegexp(`^/${TEST_PREFIX}/`),
    })
  })

  /**
   * Verifies that the Admin API URL SSM parameter is stored under the
   * /{PREFIX}/ namespace.
   */
  test("Admin API URL SSM parameter uses /{PREFIX}/ namespace", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockTable = createMockDynamoTable(parentStack, TEST_PREFIX)

    new AdminApiStack(parentStack, "AdminApi", {
      prefix: TEST_PREFIX,
      userPoolId: "us-east-1_TestPoolId",
      severityExamplesTable: mockTable,
    })

    const template = Template.fromStack(parentStack)

    // Find all SSM parameters and verify they use the /{PREFIX}/ namespace
    const ssmParams = template.findResources("AWS::SSM::Parameter")
    const paramNames = Object.values(ssmParams).map(
      (resource: any) => resource.Properties.Name as string
    )

    // Every SSM parameter should start with /{PREFIX}/
    for (const paramName of paramNames) {
      expect(paramName).toMatch(new RegExp(`^/${TEST_PREFIX}/`))
    }
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// 4. Config accepts all required parameters
//    Validates: Requirement 7.4
// ─────────────────────────────────────────────────────────────────────────────

describe("Config accepts all required parameters (Req 7.4)", () => {
  /**
   * Verifies that the ConfigManager correctly parses the config.yaml file
   * and exposes the eks_log_alerts section with all required fields.
   */
  test("ConfigManager parses eks_log_alerts section from config.yaml", () => {
    const configPath = path.join(__dirname, "..", "config.yaml")
    const configManager = new ConfigManager(configPath)
    const config: AppConfig = configManager.getProps()

    // eks_log_alerts section exists
    expect(config.eks_log_alerts).toBeDefined()

    // All required EKS log alerts fields are present
    expect(config.eks_log_alerts!.monitored_log_groups).toBeDefined()
    expect(config.eks_log_alerts!.confidence_threshold).toBeDefined()
    expect(config.eks_log_alerts!.log_poll_interval_seconds).toBeDefined()
    expect(config.eks_log_alerts!.classification_model_id).toBeDefined()
  })

  /**
   * Verifies that the ConfigManager.get() method can access nested
   * eks_log_alerts configuration values using dot notation.
   */
  test("ConfigManager.get() accesses eks_log_alerts values via dot notation", () => {
    const configPath = path.join(__dirname, "..", "config.yaml")
    const configManager = new ConfigManager(configPath)

    expect(configManager.get("eks_log_alerts.monitored_log_groups")).toBeDefined()
    expect(configManager.get("eks_log_alerts.confidence_threshold")).toBeDefined()
    expect(configManager.get("eks_log_alerts.log_poll_interval_seconds")).toBeDefined()
    expect(configManager.get("eks_log_alerts.classification_model_id")).toBeDefined()
  })

  /**
   * Verifies that the config.yaml contains the required base configuration
   * fields (stack_name_base, backend pattern, deployment_type).
   */
  test("ConfigManager parses base configuration fields", () => {
    const configPath = path.join(__dirname, "..", "config.yaml")
    const configManager = new ConfigManager(configPath)
    const config: AppConfig = configManager.getProps()

    expect(config.stack_name_base).toBeDefined()
    expect(config.stack_name_base.length).toBeGreaterThan(0)
    expect(config.backend.pattern).toBeDefined()
    expect(config.backend.deployment_type).toBeDefined()
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// 5. Env vars passed to Runtime and Lambdas
//    Validates: Requirement 9.3
// ─────────────────────────────────────────────────────────────────────────────

describe("Env vars passed to Runtime and Lambdas (Req 9.3)", () => {
  /**
   * Verifies that the Admin API Lambda receives TABLE_NAME, PREFIX, and
   * CORS_ALLOWED_ORIGINS environment variables.
   */
  test("Admin API Lambda receives required environment variables", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockTable = createMockDynamoTable(parentStack, TEST_PREFIX)

    new AdminApiStack(parentStack, "AdminApi", {
      prefix: TEST_PREFIX,
      userPoolId: "us-east-1_TestPoolId",
      severityExamplesTable: mockTable,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${TEST_PREFIX}-admin-api`,
      Environment: {
        Variables: Match.objectLike({
          TABLE_NAME: Match.anyValue(),
          PREFIX: TEST_PREFIX,
          CORS_ALLOWED_ORIGINS: Match.anyValue(),
        }),
      },
    })
  })

  /**
   * Verifies that the Slack webhook Lambda receives the
   * SLACK_CHANNEL_WEBHOOK_URL environment variable.
   */
  test("Slack webhook Lambda receives SLACK_CHANNEL_WEBHOOK_URL env var", () => {
    const [_app, parentStack] = createTestParentStack()
    const mockGatewayRole = new iam.Role(parentStack, "MockGatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    })

    const testWebhookUrl = "https://hooks.slack.com/services/T00/B00/xxx"

    new SlackWebhookStack(parentStack, "SlackWebhook", {
      prefix: TEST_PREFIX,
      slackChannelWebhookUrl: testWebhookUrl,
      gatewayId: "test-gateway-id",
      gatewayRole: mockGatewayRole,
    })

    const template = Template.fromStack(parentStack)

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${TEST_PREFIX}-slack-webhook-tool`,
      Environment: {
        Variables: Match.objectLike({
          SLACK_CHANNEL_WEBHOOK_URL: testWebhookUrl,
        }),
      },
    })
  })
})
