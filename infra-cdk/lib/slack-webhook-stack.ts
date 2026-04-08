import * as cdk from "aws-cdk-lib"
import * as iam from "aws-cdk-lib/aws-iam"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as logs from "aws-cdk-lib/aws-logs"
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore"
import { Construct } from "constructs"
import * as path from "path"
import * as fs from "fs"

/**
 * Properties for the SlackWebhookStack.
 *
 * @property prefix - Resource name prefix (e.g. "team5"). Used to namespace
 *   all resource names and log groups.
 * @property slackChannelWebhookUrl - The Slack incoming webhook URL that the
 *   Lambda will POST messages to. Passed as an environment variable.
 * @property gatewayId - The AgentCore Gateway identifier from the BackendStack.
 *   Used to register this Lambda as a new Gateway target.
 * @property gatewayRole - The IAM role used by the AgentCore Gateway. This
 *   stack grants the role permission to invoke the Slack webhook Lambda.
 */
export interface SlackWebhookStackProps extends cdk.NestedStackProps {
  /** Resource name prefix — must be provided by the caller. */
  readonly prefix: string
  /** Slack incoming webhook URL for the target channel. */
  readonly slackChannelWebhookUrl: string
  /** AgentCore Gateway identifier from the BackendStack. */
  readonly gatewayId: string
  /** IAM role used by the AgentCore Gateway — granted Lambda invoke permission. */
  readonly gatewayRole: iam.IRole
}

/**
 * CDK nested stack that provisions the Slack_Webhook_Tool as an AgentCore
 * Gateway Lambda target.
 *
 * Resources created:
 *   - Lambda function  `{prefix}-slack-webhook-tool`
 *       Handler: `gateway/tools/slack_webhook/slack_webhook_lambda.py`
 *       Environment: SLACK_CHANNEL_WEBHOOK_URL
 *   - CfnGatewayTarget  `slack-webhook-target`
 *       Registers the Lambda as a Gateway target with the tool schema
 *       from `gateway/tools/slack_webhook/tool_spec.json`
 *   - CloudWatch Log Group  `/aws/lambda/{prefix}-slack-webhook-tool`
 *
 * The Gateway role is granted invoke permission on the Lambda so that
 * AgentCore Gateway can call it when agents use the `slack_webhook_tool`.
 *
 * Follows the FAST template patterns:
 *   - Nested stack (same as BackendStack, SeverityExamplesStack, AdminApiStack)
 *   - Lambda target pattern from GATEWAY.md
 *   - CfnGatewayTarget L1 construct (same as sample tool in BackendStack)
 *   - Tool spec loaded from JSON file
 *
 * Requirements satisfied: 7.1, 7.2
 */
export class SlackWebhookStack extends cdk.NestedStack {
  /** ARN of the Slack webhook Lambda function. */
  public readonly lambdaArn: string

  /** The Lambda function construct — exposed so the main stack can grant invoke. */
  public readonly lambdaFunction: lambda.Function

  constructor(scope: Construct, id: string, props: SlackWebhookStackProps) {
    super(scope, id, props)

    const { prefix, slackChannelWebhookUrl, gatewayId, gatewayRole } = props

    // ── Lambda function ─────────────────────────────────────────────────
    const slackWebhookLambda = this.createSlackWebhookLambda(
      prefix,
      slackChannelWebhookUrl
    )
    this.lambdaArn = slackWebhookLambda.functionArn
    this.lambdaFunction = slackWebhookLambda

    // ── Grant Gateway role permission via resource-based policy ─────────
    // Uses a Lambda resource policy instead of modifying the IAM role to
    // avoid circular cross-nested-stack references between this stack and
    // BackendStack (which owns the gatewayRole).
    slackWebhookLambda.addPermission("GatewayRoleInvoke", {
      principal: new iam.ArnPrincipal(gatewayRole.roleArn),
      action: "lambda:InvokeFunction",
    })

    // ── Register as AgentCore Gateway target ────────────────────────────
    this.createGatewayTarget(gatewayId, slackWebhookLambda)
  }

  /**
   * Creates the Slack_Webhook_Tool Lambda function.
   *
   * Uses the standard Lambda construct (not PythonFunction) because the
   * handler is a single-file Python script with no external dependencies
   * beyond the standard library — matching the sample_tool pattern in
   * BackendStack.
   *
   * Environment variables passed to the Lambda:
   *   - SLACK_CHANNEL_WEBHOOK_URL: The Slack incoming webhook URL
   *
   * @param prefix - Resource name prefix (e.g. "team5")
   * @param slackChannelWebhookUrl - The Slack incoming webhook URL
   * @returns The Lambda Function construct
   */
  private createSlackWebhookLambda(
    prefix: string,
    slackChannelWebhookUrl: string
  ): lambda.Function {
    return new lambda.Function(this, "SlackWebhookToolLambda", {
      functionName: `${prefix}-slack-webhook-tool`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "slack_webhook_lambda.handler",
      // Points to gateway/tools/slack_webhook/ which contains slack_webhook_lambda.py
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "gateway", "tools", "slack_webhook") // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
      ),
      environment: {
        SLACK_CHANNEL_WEBHOOK_URL: slackChannelWebhookUrl,
      },
      timeout: cdk.Duration.seconds(30),
      logGroup: new logs.LogGroup(this, "SlackWebhookToolLambdaLogGroup", {
        logGroupName: `/aws/lambda/${prefix}-slack-webhook-tool`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })
  }

  /**
   * Registers the Slack webhook Lambda as an AgentCore Gateway target.
   *
   * Loads the tool schema from `gateway/tools/slack_webhook/tool_spec.json`
   * and creates a CfnGatewayTarget L1 construct that binds the Lambda to
   * the existing AgentCore Gateway.
   *
   * The target name `slack-webhook-target` is used as the prefix in the
   * tool name routing (e.g. `slack-webhook-target___slack_webhook_tool`).
   *
   * @param gatewayId - The AgentCore Gateway identifier
   * @param slackWebhookLambda - The Lambda function to register as a target
   */
  private createGatewayTarget(
    gatewayId: string,
    slackWebhookLambda: lambda.Function
  ): void {
    // Load tool specification from JSON file
    const toolSpecPath = path.join(
      __dirname,
      "..",
      "..",
      "gateway",
      "tools",
      "slack_webhook",
      "tool_spec.json"
    ) // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
    const toolSpec = JSON.parse(fs.readFileSync(toolSpecPath, "utf8"))

    const gatewayTarget = new bedrockagentcore.CfnGatewayTarget(
      this,
      "SlackWebhookGatewayTarget",
      {
        gatewayIdentifier: gatewayId,
        name: "slack-webhook-target",
        description: "Slack_Webhook_Tool Lambda target — posts formatted messages to Slack",
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: slackWebhookLambda.functionArn,
              toolSchema: {
                inlinePayload: toolSpec,
              },
            },
          },
        },
        credentialProviderConfigurations: [
          {
            credentialProviderType: "GATEWAY_IAM_ROLE",
          },
        ],
      }
    )

    // Ensure the Lambda exists before the target is created
    gatewayTarget.node.addDependency(slackWebhookLambda)
  }
}
