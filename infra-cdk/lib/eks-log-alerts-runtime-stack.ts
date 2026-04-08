import * as cdk from "aws-cdk-lib"
import * as dynamodb from "aws-cdk-lib/aws-dynamodb"
import * as iam from "aws-cdk-lib/aws-iam"
import * as ssm from "aws-cdk-lib/aws-ssm"
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets"
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha"
import { Construct } from "constructs"
import { AgentCoreRole } from "./utils/agentcore-role"
import * as path from "path"

/**
 * Properties for the EksLogAlertsRuntimeStack.
 *
 * Every property is required — the stack fails loudly if any value is
 * missing rather than falling back to defaults.
 *
 * @property prefix - Resource name prefix (e.g. "team5"). Used to namespace
 *   all resource names, SSM parameters, and environment variables.
 * @property stackNameBase - The CDK stack name base from config.yaml. Passed
 *   as the STACK_NAME env var so the agent can look up SSM parameters.
 * @property severityExamplesTable - The DynamoDB Table construct from the
 *   SeverityExamplesStack. The Runtime role is granted read access.
 * @property gatewayCredentialProviderName - The OAuth2 credential provider
 *   name used by the agent to authenticate with the AgentCore Gateway.
 * @property monitoredLogGroups - Comma-separated CloudWatch log group names
 *   to monitor for errors.
 * @property confidenceThreshold - Float string (e.g. "0.7") — minimum
 *   confidence for auto-classification.
 * @property logPollIntervalSeconds - Integer string (e.g. "30") — polling
 *   interval in seconds.
 * @property classificationModelId - Bedrock model ID for the Classification_Agent.
 * @property slackChannelWebhookUrl - Slack incoming webhook URL — stored in
 *   SSM for the agent to read at runtime.
 * @property userPoolId - Cognito User Pool ID for JWT authorizer configuration.
 * @property userPoolClientId - Cognito User Pool Client ID for JWT audience.
 */
export interface EksLogAlertsRuntimeStackProps extends cdk.NestedStackProps {
  readonly prefix: string
  readonly stackNameBase: string
  readonly severityExamplesTable: dynamodb.Table
  readonly gatewayCredentialProviderName: string
  readonly monitoredLogGroups: string
  readonly confidenceThreshold: string
  readonly logPollIntervalSeconds: string
  readonly classificationModelId: string
  readonly slackChannelWebhookUrl: string
  readonly userPoolId: string
  readonly userPoolClientId: string
}

/**
 * CDK nested stack that registers the EKS Log Alerts Orchestrator Agent
 * with AgentCore Runtime and stores all configuration values in SSM
 * Parameter Store.
 *
 * Resources created:
 *   - AgentCore Runtime  `{prefix}_EksLogAlertsAgent`
 *       Docker-based deployment using `patterns/eks-log-alerts/Dockerfile`
 *       Environment variables: MONITORED_LOG_GROUPS, CONFIDENCE_THRESHOLD,
 *         LOG_POLL_INTERVAL_SECONDS, CLASSIFICATION_MODEL_ID,
 *         SEVERITY_EXAMPLES_TABLE_NAME, PREFIX, STACK_NAME,
 *         GATEWAY_CREDENTIAL_PROVIDER_NAME, SLACK_CHANNEL_WEBHOOK_URL,
 *         AWS_REGION, AWS_DEFAULT_REGION
 *   - AgentCoreRole with permissions for:
 *       Bedrock model invocation, CloudWatch Logs read, DynamoDB read,
 *       SSM parameter access, OAuth2 credential provider access,
 *       Secrets Manager access
 *   - SSM parameters under `/{prefix}/` namespace:
 *       - `/{prefix}/eks-log-alerts-runtime-arn`
 *       - `/{prefix}/monitored-log-groups`
 *       - `/{prefix}/confidence-threshold`
 *       - `/{prefix}/log-poll-interval-seconds`
 *       - `/{prefix}/classification-model-id`
 *       - `/{prefix}/slack-channel-webhook-url`
 *
 * Follows the FAST template patterns:
 *   - Nested stack (same as BackendStack, SeverityExamplesStack, etc.)
 *   - AgentCoreRole utility for execution role
 *   - Docker-based AgentRuntimeArtifact
 *   - JWT authorizer with Cognito
 *   - SSM parameters for service discovery
 *
 * Requirements satisfied: 7.1, 7.2, 7.3, 7.4, 7.5, 9.3
 */
export class EksLogAlertsRuntimeStack extends cdk.NestedStack {
  /** ARN of the registered AgentCore Runtime. */
  public readonly runtimeArn: string

  constructor(scope: Construct, id: string, props: EksLogAlertsRuntimeStackProps) {
    super(scope, id, props)

    const {
      prefix,
      stackNameBase,
      severityExamplesTable,
      gatewayCredentialProviderName,
      monitoredLogGroups,
      confidenceThreshold,
      logPollIntervalSeconds,
      classificationModelId,
      slackChannelWebhookUrl,
      userPoolId,
      userPoolClientId,
    } = props

    // ── AgentCore Runtime ───────────────────────────────────────────────
    const agentRole = this.createAgentRole({
      stackNameBase,
      prefix,
      severityExamplesTable,
    })

    const runtime = this.createRuntime({
      prefix,
      stackNameBase,
      agentRole,
      severityExamplesTable,
      gatewayCredentialProviderName,
      monitoredLogGroups,
      confidenceThreshold,
      logPollIntervalSeconds,
      classificationModelId,
      slackChannelWebhookUrl,
      userPoolId,
      userPoolClientId,
    })

    this.runtimeArn = runtime.agentRuntimeArn

    // ── SSM parameters ──────────────────────────────────────────────────
    this.storeConfigurationParameters({
      prefix,
      runtimeArn: this.runtimeArn,
      monitoredLogGroups,
      confidenceThreshold,
      logPollIntervalSeconds,
      classificationModelId,
      slackChannelWebhookUrl,
    })

    // ── Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "EksLogAlertsRuntimeArn", {
      description: "ARN of the EKS Log Alerts AgentCore Runtime",
      value: this.runtimeArn,
    })

    new cdk.CfnOutput(this, "EksLogAlertsRuntimeId", {
      description: "ID of the EKS Log Alerts AgentCore Runtime",
      value: runtime.agentRuntimeId,
    })
  }

  /**
   * Creates the AgentCore execution role with all permissions required by
   * the EKS Log Alerts Orchestrator Agent.
   *
   * Permissions granted beyond the base AgentCoreRole:
   *   - CloudWatch Logs: FilterLogEvents, DescribeLogGroups (for log polling)
   *   - DynamoDB: Read access to the severity examples table
   *   - SSM: GetParameter for /{stackNameBase}/* and /{prefix}/* namespaces
   *   - OAuth2: GetOauth2CredentialProvider, GetResourceOauth2Token
   *   - Secrets Manager: GetSecretValue for machine client and token vault secrets
   *
   * @param params.stackNameBase - CDK stack name base for SSM parameter scoping
   * @param params.prefix - Resource name prefix for SSM parameter scoping
   * @param params.severityExamplesTable - DynamoDB table to grant read access to
   * @returns The configured AgentCoreRole
   */
  private createAgentRole(params: {
    stackNameBase: string
    prefix: string
    severityExamplesTable: dynamodb.Table
  }): AgentCoreRole {
    const { stackNameBase, prefix, severityExamplesTable } = params

    const agentRole = new AgentCoreRole(this, "EksLogAlertsAgentRole")

    // CloudWatch Logs read access — the Log_Ingestion_Agent polls log groups
    // for error events using FilterLogEvents
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogsReadAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "logs:FilterLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:*`,
        ],
      })
    )

    // DynamoDB read access — the Classification_Agent reads severity examples
    // for few-shot classification prompts
    severityExamplesTable.grantReadData(agentRole)

    // SSM parameter access — the agent reads Gateway URL and other config
    // from both the stack_name_base namespace and the prefix namespace
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SSMParameterAccess",
        effect: iam.Effect.ALLOW,
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${stackNameBase}/*`,
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${prefix}/*`,
        ],
      })
    )

    // OAuth2 Credential Provider access — the agent authenticates with the
    // AgentCore Gateway using the @requires_access_token decorator
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OAuth2CredentialProviderAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:GetOauth2CredentialProvider",
          "bedrock-agentcore:GetResourceOauth2Token",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:oauth2-credential-provider/*`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/*`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/*`,
        ],
      })
    )

    // Secrets Manager access — the agent reads the machine client secret
    // and the Token Vault OAuth2 secret for Gateway authentication
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SecretsManagerOAuth2Access",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:/${stackNameBase}/machine_client_secret*`,
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore-identity!default/oauth2/${stackNameBase}-runtime-gateway-auth*`,
        ],
      })
    )

    return agentRole
  }

  /**
   * Creates the AgentCore Runtime for the EKS Log Alerts Orchestrator Agent.
   *
   * Uses Docker-based deployment with the `patterns/eks-log-alerts/Dockerfile`.
   * Configures JWT authorization via Cognito and passes all required
   * environment variables to the runtime container.
   *
   * Environment variables passed to the Runtime:
   *   - AWS_REGION, AWS_DEFAULT_REGION: AWS region for SDK calls
   *   - MONITORED_LOG_GROUPS: Comma-separated CloudWatch log group names
   *   - CONFIDENCE_THRESHOLD: Float classification confidence threshold
   *   - LOG_POLL_INTERVAL_SECONDS: Polling interval in seconds
   *   - CLASSIFICATION_MODEL_ID: Bedrock model ID for classification
   *   - SEVERITY_EXAMPLES_TABLE_NAME: DynamoDB table name
   *   - PREFIX: Resource name prefix
   *   - STACK_NAME: CDK stack name base for SSM lookups
   *   - GATEWAY_CREDENTIAL_PROVIDER_NAME: OAuth2 provider for Gateway auth
   *   - SLACK_CHANNEL_WEBHOOK_URL: Slack incoming webhook URL
   *
   * @param params - All required configuration values for the Runtime
   * @returns The AgentCore Runtime construct
   */
  private createRuntime(params: {
    prefix: string
    stackNameBase: string
    agentRole: AgentCoreRole
    severityExamplesTable: dynamodb.Table
    gatewayCredentialProviderName: string
    monitoredLogGroups: string
    confidenceThreshold: string
    logPollIntervalSeconds: string
    classificationModelId: string
    slackChannelWebhookUrl: string
    userPoolId: string
    userPoolClientId: string
  }): agentcore.Runtime {
    const stack = cdk.Stack.of(this)

    // Build the Docker image from the repository root using the
    // eks-log-alerts Dockerfile (same pattern as strands-single-agent)
    const agentRuntimeArtifact = agentcore.AgentRuntimeArtifact.fromAsset(
      path.resolve(__dirname, "..", ".."), // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
      {
        platform: ecr_assets.Platform.LINUX_ARM64,
        file: "patterns/eks-log-alerts/Dockerfile",
      }
    )

    // Configure JWT authorizer with Cognito — same pattern as BackendStack
    const authorizerConfiguration = agentcore.RuntimeAuthorizerConfiguration.usingJWT(
      `https://cognito-idp.${stack.region}.amazonaws.com/${params.userPoolId}/.well-known/openid-configuration`,
      [params.userPoolClientId]
    )

    // All environment variables required by the Orchestrator Agent
    // (see patterns/eks-log-alerts/config.py for the full list)
    const envVars: { [key: string]: string } = {
      AWS_REGION: stack.region,
      AWS_DEFAULT_REGION: stack.region,
      MONITORED_LOG_GROUPS: params.monitoredLogGroups,
      CONFIDENCE_THRESHOLD: params.confidenceThreshold,
      LOG_POLL_INTERVAL_SECONDS: params.logPollIntervalSeconds,
      CLASSIFICATION_MODEL_ID: params.classificationModelId,
      SEVERITY_EXAMPLES_TABLE_NAME: params.severityExamplesTable.tableName,
      PREFIX: params.prefix,
      STACK_NAME: params.stackNameBase,
      GATEWAY_CREDENTIAL_PROVIDER_NAME: params.gatewayCredentialProviderName,
      SLACK_CHANNEL_WEBHOOK_URL: params.slackChannelWebhookUrl,
    }

    // Create the Runtime using the L2 construct — follows BackendStack pattern
    const runtime = new agentcore.Runtime(this, "EksLogAlertsRuntime", {
      runtimeName: `${params.prefix}_EksLogAlertsAgent`,
      agentRuntimeArtifact: agentRuntimeArtifact,
      executionRole: params.agentRole,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      environmentVariables: envVars,
      authorizerConfiguration: authorizerConfiguration,
      requestHeaderConfiguration: {
        allowlistedHeaders: ["Authorization"],
      },
      description: `EKS Log Alerts Orchestrator Agent runtime for ${params.prefix}`,
    })

    return runtime
  }

  /**
   * Stores all EKS Log Alerts configuration values in SSM Parameter Store
   * under the `/{prefix}/` namespace.
   *
   * This enables other components (agents, Lambdas, scripts) to discover
   * configuration at runtime without hard-coding values. Each parameter
   * includes a descriptive comment for operators browsing SSM in the console.
   *
   * Parameters stored:
   *   - `/{prefix}/eks-log-alerts-runtime-arn`: Runtime ARN for invocation
   *   - `/{prefix}/monitored-log-groups`: CloudWatch log groups to monitor
   *   - `/{prefix}/confidence-threshold`: Classification confidence threshold
   *   - `/{prefix}/log-poll-interval-seconds`: Polling interval
   *   - `/{prefix}/classification-model-id`: Bedrock model ID
   *   - `/{prefix}/slack-channel-webhook-url`: Slack webhook URL
   *
   * @param params - Configuration values to store in SSM
   */
  private storeConfigurationParameters(params: {
    prefix: string
    runtimeArn: string
    monitoredLogGroups: string
    confidenceThreshold: string
    logPollIntervalSeconds: string
    classificationModelId: string
    slackChannelWebhookUrl: string
  }): void {
    const { prefix } = params

    new ssm.StringParameter(this, "EksLogAlertsRuntimeArnParam", {
      parameterName: `/${prefix}/eks-log-alerts-runtime-arn`,
      stringValue: params.runtimeArn,
      description: "AgentCore Runtime ARN for the EKS Log Alerts Orchestrator Agent",
    })

    new ssm.StringParameter(this, "MonitoredLogGroupsParam", {
      parameterName: `/${prefix}/monitored-log-groups`,
      stringValue: params.monitoredLogGroups,
      description: "Comma-separated CloudWatch log group names monitored for errors",
    })

    new ssm.StringParameter(this, "ConfidenceThresholdParam", {
      parameterName: `/${prefix}/confidence-threshold`,
      stringValue: params.confidenceThreshold,
      description: "Minimum confidence score (0.0–1.0) for auto-classification",
    })

    new ssm.StringParameter(this, "LogPollIntervalSecondsParam", {
      parameterName: `/${prefix}/log-poll-interval-seconds`,
      stringValue: params.logPollIntervalSeconds,
      description: "Polling interval in seconds for CloudWatch log monitoring",
    })

    new ssm.StringParameter(this, "ClassificationModelIdParam", {
      parameterName: `/${prefix}/classification-model-id`,
      stringValue: params.classificationModelId,
      description: "Bedrock model ID used by the Classification_Agent",
    })

    new ssm.StringParameter(this, "SlackChannelWebhookUrlParam", {
      parameterName: `/${prefix}/slack-channel-webhook-url`,
      stringValue: params.slackChannelWebhookUrl,
      description: "Slack incoming webhook URL for critical error notifications",
    })
  }
}
