import * as cdk from "aws-cdk-lib"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"

// Import nested stacks
import { BackendStack } from "./backend-stack"
import { AmplifyHostingStack } from "./amplify-hosting-stack"
import { CognitoStack } from "./cognito-stack"
import { SeverityExamplesStack } from "./severity-examples-stack"
import { AdminApiStack } from "./admin-api-stack"
import { SlackWebhookStack } from "./slack-webhook-stack"
import { EksLogAlertsRuntimeStack } from "./eks-log-alerts-runtime-stack"

export interface FastAmplifyStackProps extends cdk.StackProps {
  config: AppConfig
}

export class FastMainStack extends cdk.Stack {
  public readonly amplifyHostingStack: AmplifyHostingStack
  public readonly backendStack: BackendStack
  public readonly cognitoStack: CognitoStack
  public readonly severityExamplesStack: SeverityExamplesStack
  public readonly adminApiStack: AdminApiStack
  public readonly slackWebhookStack: SlackWebhookStack
  public readonly eksLogAlertsRuntimeStack: EksLogAlertsRuntimeStack

  constructor(scope: Construct, id: string, props: FastAmplifyStackProps) {
    const description =
      "Fullstack AgentCore Solution Template - Main Stack (v0.4.1) (uksb-v6dos0t5g8)"
    super(scope, id, { ...props, description })

    // Step 1: Create the Amplify stack to get the predictable domain
    this.amplifyHostingStack = new AmplifyHostingStack(this, `${id}-amplify`, {
      config: props.config,
    })

    this.cognitoStack = new CognitoStack(this, `${id}-cognito`, {
      config: props.config,
      callbackUrls: ["http://localhost:3000", this.amplifyHostingStack.amplifyUrl],
    })

    // Step 2: Create backend stack with the predictable Amplify URL and Cognito details
    this.backendStack = new BackendStack(this, `${id}-backend`, {
      config: props.config,
      userPoolId: this.cognitoStack.userPoolId,
      userPoolClientId: this.cognitoStack.userPoolClientId,
      userPoolDomain: this.cognitoStack.userPoolDomain,
      frontendUrl: this.amplifyHostingStack.amplifyUrl,
    })

    // Step 3: Create the Severity Examples DynamoDB table for EKS log alerts
    // Uses PREFIX env var (default "team5") for resource naming and SSM namespace
    const prefix = process.env.PREFIX ?? "team5"
    this.severityExamplesStack = new SeverityExamplesStack(
      this,
      `${id}-severity-examples`,
      { prefix }
    )

    // Step 4: Create the Admin API for severity examples CRUD
    // Uses Cognito authorizer from the Cognito stack and the DynamoDB table
    // from the Severity Examples stack
    this.adminApiStack = new AdminApiStack(this, `${id}-admin-api`, {
      prefix,
      userPoolId: this.cognitoStack.userPoolId,
      severityExamplesTable: this.severityExamplesStack.table,
    })

    // Step 5: Create the Slack Webhook Tool as an AgentCore Gateway Lambda target
    // Reads SLACK_CHANNEL_WEBHOOK_URL from environment — fails loudly if missing
    const slackChannelWebhookUrl = process.env.SLACK_CHANNEL_WEBHOOK_URL
    if (!slackChannelWebhookUrl) {
      throw new Error(
        "Missing required environment variable: SLACK_CHANNEL_WEBHOOK_URL. " +
          "Set it before running cdk deploy (e.g. export SLACK_CHANNEL_WEBHOOK_URL=https://hooks.slack.com/services/...)."
      )
    }
    this.slackWebhookStack = new SlackWebhookStack(
      this,
      `${id}-slack-webhook`,
      {
        prefix,
        slackChannelWebhookUrl,
        gatewayId: this.backendStack.gatewayId,
        gatewayRole: this.backendStack.gatewayRole,
      }
    )

    // Step 6: Create the EKS Log Alerts AgentCore Runtime
    // Reads configurable parameters from config.yaml with environment variable overrides.
    // Environment variables take precedence over config.yaml values.
    const eksLogAlertsConfig = props.config.eks_log_alerts
    const monitoredLogGroups =
      process.env.MONITORED_LOG_GROUPS ?? eksLogAlertsConfig?.monitored_log_groups
    if (!monitoredLogGroups) {
      throw new Error(
        "Missing required configuration: MONITORED_LOG_GROUPS. " +
          "Set it via environment variable or in config.yaml under eks_log_alerts.monitored_log_groups."
      )
    }
    const confidenceThreshold =
      process.env.CONFIDENCE_THRESHOLD ?? eksLogAlertsConfig?.confidence_threshold
    if (!confidenceThreshold) {
      throw new Error(
        "Missing required configuration: CONFIDENCE_THRESHOLD. " +
          "Set it via environment variable or in config.yaml under eks_log_alerts.confidence_threshold."
      )
    }
    const logPollIntervalSeconds =
      process.env.LOG_POLL_INTERVAL_SECONDS ?? eksLogAlertsConfig?.log_poll_interval_seconds
    if (!logPollIntervalSeconds) {
      throw new Error(
        "Missing required configuration: LOG_POLL_INTERVAL_SECONDS. " +
          "Set it via environment variable or in config.yaml under eks_log_alerts.log_poll_interval_seconds."
      )
    }
    const classificationModelId =
      process.env.CLASSIFICATION_MODEL_ID ?? eksLogAlertsConfig?.classification_model_id
    if (!classificationModelId) {
      throw new Error(
        "Missing required configuration: CLASSIFICATION_MODEL_ID. " +
          "Set it via environment variable or in config.yaml under eks_log_alerts.classification_model_id."
      )
    }

    // The gateway credential provider name follows the FAST convention:
    // {stack_name_base}-runtime-gateway-auth (created by BackendStack)
    const gatewayCredentialProviderName = `${props.config.stack_name_base}-runtime-gateway-auth`

    this.eksLogAlertsRuntimeStack = new EksLogAlertsRuntimeStack(
      this,
      `${id}-eks-log-alerts-runtime`,
      {
        prefix,
        stackNameBase: props.config.stack_name_base,
        severityExamplesTable: this.severityExamplesStack.table,
        gatewayCredentialProviderName,
        monitoredLogGroups,
        confidenceThreshold,
        logPollIntervalSeconds,
        classificationModelId,
        slackChannelWebhookUrl,
        userPoolId: this.cognitoStack.userPoolId,
        userPoolClientId: this.cognitoStack.userPoolClientId,
      }
    )

    // Outputs
    new cdk.CfnOutput(this, "AmplifyAppId", {
      value: this.amplifyHostingStack.amplifyApp.appId,
      description: "Amplify App ID - use this for manual deployment",
      exportName: `${props.config.stack_name_base}-AmplifyAppId`,
    })

    new cdk.CfnOutput(this, "CognitoUserPoolId", {
      value: this.cognitoStack.userPoolId,
      description: "Cognito User Pool ID",
      exportName: `${props.config.stack_name_base}-CognitoUserPoolId`,
    })

    new cdk.CfnOutput(this, "CognitoClientId", {
      value: this.cognitoStack.userPoolClientId,
      description: "Cognito User Pool Client ID",
      exportName: `${props.config.stack_name_base}-CognitoClientId`,
    })

    new cdk.CfnOutput(this, "CognitoDomain", {
      value: `${this.cognitoStack.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
      description: "Cognito Domain for OAuth",
      exportName: `${props.config.stack_name_base}-CognitoDomain`,
    })

    new cdk.CfnOutput(this, "RuntimeArn", {
      value: this.backendStack.runtimeArn,
      description: "AgentCore Runtime ARN",
      exportName: `${props.config.stack_name_base}-RuntimeArn`,
    })

    new cdk.CfnOutput(this, "MemoryArn", {
      value: this.backendStack.memoryArn,
      description: "AgentCore Memory ARN",
      exportName: `${props.config.stack_name_base}-MemoryArn`,
    })

    new cdk.CfnOutput(this, "FeedbackApiUrl", {
      value: this.backendStack.feedbackApiUrl,
      description: "Feedback API Gateway URL",
      exportName: `${props.config.stack_name_base}-FeedbackApiUrl`,
    })

    new cdk.CfnOutput(this, "AmplifyConsoleUrl", {
      value: `https://console.aws.amazon.com/amplify/apps/${this.amplifyHostingStack.amplifyApp.appId}`,
      description: "Amplify Console URL for monitoring deployments",
    })

    new cdk.CfnOutput(this, "AmplifyUrl", {
      value: this.amplifyHostingStack.amplifyUrl,
      description: "Amplify Frontend URL (available after deployment)",
    })

    new cdk.CfnOutput(this, "StagingBucketName", {
      value: this.amplifyHostingStack.stagingBucket.bucketName,
      description: "S3 bucket for Amplify deployment staging",
      exportName: `${props.config.stack_name_base}-StagingBucket`,
    })

    new cdk.CfnOutput(this, "SeverityExamplesTableName", {
      value: this.severityExamplesStack.tableName,
      description: "DynamoDB table name for severity classification examples",
      exportName: `${props.config.stack_name_base}-SeverityExamplesTableName`,
    })

    new cdk.CfnOutput(this, "AdminApiUrl", {
      value: this.adminApiStack.apiUrl,
      description: "Admin API Gateway URL for severity examples CRUD",
      exportName: `${props.config.stack_name_base}-AdminApiUrl`,
    })

    new cdk.CfnOutput(this, "SlackWebhookLambdaArn", {
      value: this.slackWebhookStack.lambdaArn,
      description: "ARN of the Slack_Webhook_Tool Lambda (Gateway target)",
      exportName: `${props.config.stack_name_base}-SlackWebhookLambdaArn`,
    })

    new cdk.CfnOutput(this, "EksLogAlertsRuntimeArn", {
      value: this.eksLogAlertsRuntimeStack.runtimeArn,
      description: "AgentCore Runtime ARN for the EKS Log Alerts Orchestrator Agent",
      exportName: `${props.config.stack_name_base}-EksLogAlertsRuntimeArn`,
    })
  }
}
