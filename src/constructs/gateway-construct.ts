import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';
import { NagSuppressions } from 'cdk-nag';

export interface GatewayConstructProps {
  readonly stage: string;
  readonly prefix: string;
  readonly boto3Layer: lambda.ILayerVersion;
  readonly glueDatabaseName: string;
  readonly athenaWorkGroupName: string;
  readonly athenaResultsBucketName: string;
  readonly rawDataBucketArn: string;
}

interface ToolSpec {
  name: string;
  /** Short description for the Gateway Target (max 200 chars). */
  description: string;
  /** Richer description for the tool schema the agent sees (no limit). */
  toolDescription: string;
  handler: string;
  inputSchema: agentcore.CfnGatewayTarget.SchemaDefinitionProperty;
  envVars?: Record<string, string>;
  extraPolicies?: iam.PolicyStatement[];
}

export class GatewayConstruct extends Construct {
  public readonly gatewayUrl: string;
  public readonly gatewayArn: string;

  constructor(scope: Construct, id: string, props: GatewayConstructProps) {
    super(scope, id);

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;
    const { stage, prefix } = props;

    // Shared env vars for Athena tools
    const athenaEnv: Record<string, string> = {
      DATABASE_NAME: props.glueDatabaseName,
      WORKGROUP: props.athenaWorkGroupName,
      OUTPUT_LOCATION: `s3://${props.athenaResultsBucketName}/query-results/`,
    };

    // Scoped Athena + AWS Glue + S3 policy for query tools
    const athenaPolicy = new iam.PolicyStatement({
      actions: [
        'athena:StartQueryExecution', 'athena:GetQueryExecution',
        'athena:GetQueryResults', 'athena:StopQueryExecution',
        'athena:GetWorkGroup',
      ],
      resources: [
        `arn:aws:athena:${region}:${account}:workgroup/${props.athenaWorkGroupName}`,
      ],
    });

    const gluePolicy = new iam.PolicyStatement({
      actions: [
        'glue:GetDatabase', 'glue:GetDatabases', 'glue:GetTable', 'glue:GetTables',
        'glue:GetPartitions',
      ],
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/${props.glueDatabaseName}`,
        `arn:aws:glue:${region}:${account}:table/${props.glueDatabaseName}/*`,
      ],
    });

    const s3Policy = new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket', 's3:GetBucketLocation', 's3:PutObject'],
      resources: [
        props.rawDataBucketArn,
        `${props.rawDataBucketArn}/*`,
        `arn:aws:s3:::${props.athenaResultsBucketName}`,
        `arn:aws:s3:::${props.athenaResultsBucketName}/*`,
      ],
    });

    // ── Tool definitions ──────────────────────────────────────────
    const tools: ToolSpec[] = [
      {
        name: 'query_ses_analytics',
        description: 'Execute Athena SQL against the SES analytics database. Query email events: sends, deliveries, bounces, complaints, opens, clicks. Table: ses_events, partitioned by year/month/day/hour.',
        toolDescription: 'Execute an Athena SQL query against the SES analytics database. Use this to query email event data (sends, deliveries, bounces, complaints, opens, clicks). The database contains a table called ses_events with columns: eventtype, mail (struct with timestamp, source, messageid, destination, headers, commonheaders), delivery, bounce, complaint, open, click, reject, renderingfailure. Partitioned by year, month, day, hour.',
        handler: 'query_ses_analytics.lambda_handler',
        inputSchema: {
          type: 'object',
          properties: {
            sql_query: { type: 'string', description: 'The Athena SQL query to execute against the SES analytics database. Always use the ses_events table.' },
            force_s3: { type: 'boolean', description: 'Force results to be written to S3 even if under 500 rows. Useful for testing the large dataset workflow.' },
          },
          required: ['sql_query'],
        },
        envVars: athenaEnv,
        extraPolicies: [athenaPolicy, gluePolicy, s3Policy],
      },
      {
        name: 'describe_ses_schema',
        description: 'Describe the ses_events table schema and list available analytics views (daily_summary, bounce_analysis, etc).',
        toolDescription: 'Describe the SES analytics database schema. Without a table_name, returns the full ses_events table schema plus a list of available view names. With a table_name, returns detailed column info and sample partitions for that specific table or view. Use this before writing queries to understand the data structure.',
        handler: 'describe_schema.lambda_handler',
        inputSchema: {
          type: 'object',
          properties: {
            table_name: { type: 'string', description: 'Optional specific table name to describe. If omitted, describes all tables.' },
          },
          required: [],
        },
        envVars: athenaEnv,
        extraPolicies: [gluePolicy],
      },
      {
        name: 'get_delivery_summary',
        description: 'SES delivery metrics summary with filtering by sender, recipient, event type, and optional grouping by day/sender/event_type.',
        toolDescription: 'Get a summary of SES email delivery metrics for a given time period. Returns counts and rates for sends, deliveries, bounces, complaints, opens, and clicks. Supports filtering by sender email, recipient email, and event type. Can group results by day (daily breakdown), sender (per-sender stats), or event_type (counts per event type). Without group_by, returns a single aggregate summary with computed rates (delivery_rate, bounce_rate, complaint_rate, open_rate, click_rate).',
        handler: 'get_delivery_summary.lambda_handler',
        inputSchema: {
          type: 'object',
          properties: {
            days: { type: 'number', description: 'Number of days to look back (default 7, max 90)' },
            sender: { type: 'string', description: 'Filter by sender email address' },
            recipient: { type: 'string', description: 'Filter by recipient email address' },
            event_type: { type: 'string', description: 'Filter by event type: send, delivery, bounce, complaint, open, click, or reject' },
            group_by: { type: 'string', description: 'Group results by: day (daily breakdown), sender (per-sender stats), or event_type (counts per type). Omit for a single aggregate summary.' },
          },
          required: [],
        },
        envVars: athenaEnv,
        extraPolicies: [athenaPolicy, gluePolicy, s3Policy],
      },
    ];

    // ── Gateway service role ────────────────────────────────────────
    const gatewayRole = new iam.Role(this, 'GatewayServiceRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Service role for AgentCore MCP Gateway',
    });

    // Code Interpreter permissions
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CodeInterpreter',
      actions: [
        'bedrock-agentcore:CreateCodeInterpreter',
        'bedrock-agentcore:DeleteCodeInterpreter',
        'bedrock-agentcore:ListCodeInterpreters',
        'bedrock-agentcore:GetCodeInterpreter',
        'bedrock-agentcore:StartCodeInterpreterSession',
        'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession',
        'bedrock-agentcore:GetCodeInterpreterSession',
        'bedrock-agentcore:ListCodeInterpreterSessions',
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*`],
    }));

    gatewayRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*`],
    }));

    // ── MCP Gateway ────────────────────────────────────────────────
    const gatewayName = `${prefix}-gateway-${stage}`;
    const gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: gatewayName,
      roleArn: gatewayRole.roleArn,
      protocolType: 'MCP',
      authorizerType: 'AWS_IAM',
      protocolConfiguration: {
        mcp: { supportedVersions: ['2025-03-26'] },
      },
    });
    gateway.node.addDependency(gatewayRole);

    const gatewayId = gateway.attrGatewayIdentifier;
    this.gatewayArn = `arn:aws:bedrock-agentcore:${region}:${account}:gateway/${gatewayId}`;
    this.gatewayUrl = gateway.attrGatewayUrl;

    // ── Gateway Targets — one Lambda per tool ───────────────────────
    tools.forEach((tool, idx) => {
      const idxStr = String(idx).padStart(2, '0');
      const targetName = `t${idxStr}-${tool.name.replace(/_/g, '').substring(0, 10).padEnd(10, '0')}`;

      const fn = new lambda.Function(this, `Tool_${tool.name}`, {
        functionName: `${prefix}-gw-${tool.name.replace(/_/g, '-')}-${stage}`,
        runtime: lambda.Runtime.PYTHON_3_11,
        architecture: lambda.Architecture.ARM_64,
        handler: tool.handler,
        code: lambda.Code.fromAsset('lambda/gateway-tools'),
        timeout: cdk.Duration.seconds(120),
        memorySize: 256,
        layers: [props.boto3Layer],
        environment: tool.envVars ?? {},
        description: `Gateway tool: ${tool.name}`,
      });

      fn.grantInvoke(gatewayRole);

      // Add extra policies if defined
      tool.extraPolicies?.forEach(policy => fn.addToRolePolicy(policy));

      const target = new agentcore.CfnGatewayTarget(this, `Target${idxStr}`, {
        gatewayIdentifier: gatewayId,
        name: targetName,
        description: tool.description,
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: fn.functionArn,
              toolSchema: {
                inlinePayload: [{
                  name: tool.name,
                  description: tool.toolDescription,
                  inputSchema: tool.inputSchema,
                }],
              },
            },
          },
        },
        credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      });
      target.addDependency(gateway);
    });

    // ── Outputs ─────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'GatewayUrl', {
      value: this.gatewayUrl,
      description: 'AgentCore MCP Gateway URL',
    });
    new cdk.CfnOutput(this, 'GatewayId', {
      value: gatewayId,
      description: 'AgentCore Gateway ID',
    });

    // ── cdk-nag suppressions ────────────────────────────────────────
    const stack = cdk.Stack.of(this);

    // IAM4: AWSLambdaBasicExecutionRole is the standard CDK-managed policy for Lambda log access
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Gateway/Tool_query_ses_analytics/ServiceRole/Resource`,
      `${stack.stackName}/Gateway/Tool_describe_ses_schema/ServiceRole/Resource`,
      `${stack.stackName}/Gateway/Tool_get_delivery_summary/ServiceRole/Resource`,
    ], [{ id: 'AwsSolutions-IAM4', reason: 'AWSLambdaBasicExecutionRole is the standard CDK-managed policy for Lambda CloudWatch Logs access.' }]);

    // L1: Lambda functions use Python 3.11 to match the shared boto3 layer runtime
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Gateway/Tool_query_ses_analytics/Resource`,
      `${stack.stackName}/Gateway/Tool_describe_ses_schema/Resource`,
      `${stack.stackName}/Gateway/Tool_get_delivery_summary/Resource`,
    ], [{ id: 'AwsSolutions-L1', reason: 'Lambda functions use Python 3.11 to match the shared boto3 layer runtime compatibility.' }]);

    // IAM5: Wildcard permissions are resource-scoped and required for gateway operations
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Gateway/GatewayServiceRole/DefaultPolicy/Resource`,
      `${stack.stackName}/Gateway/Tool_query_ses_analytics/ServiceRole/DefaultPolicy/Resource`,
      `${stack.stackName}/Gateway/Tool_describe_ses_schema/ServiceRole/DefaultPolicy/Resource`,
      `${stack.stackName}/Gateway/Tool_get_delivery_summary/ServiceRole/DefaultPolicy/Resource`,
    ], [{
      id: 'AwsSolutions-IAM5',
      reason: 'Wildcard permissions are resource-scoped (bucket/*, database/table/*, lambda:*) and required for S3 object access, Glue table enumeration, Lambda invocation versions/aliases, and AgentCore code-interpreter/log-group operations.',
    }], true);
  }
}
