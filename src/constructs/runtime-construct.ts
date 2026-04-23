import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { NagSuppressions } from 'cdk-nag';
import { AppConfig } from '../config';

export interface AnalyticsResourceRefs {
  readonly glueDatabaseName: string;
  readonly athenaWorkGroupName: string;
  readonly athenaResultsBucketName: string;
  readonly rawDataBucketArn: string;
  readonly rawDataBucketName: string;
}

export interface RuntimeConstructProps {
  readonly config: AppConfig;
  readonly boto3Layer: lambda.ILayerVersion;
  readonly gatewayUrl: string;
  readonly gatewayArn: string;
  readonly analyticsResources: AnalyticsResourceRefs;
}

export class RuntimeConstruct extends Construct {
  public readonly runtimeArn: string;
  public readonly apiEndpoint: string;

  constructor(scope: Construct, id: string, props: RuntimeConstructProps) {
    super(scope, id);

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;
    const { config } = props;
    const { stage, projectName: prefix } = config;
    const agentName = 'ses-analytics';

    // ── ECR Repository ──────────────────────────────────────────────
    const ecrRepo = new ecr.Repository(this, 'AgentECR', {
      repositoryName: `${prefix}-agent`,
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: stage !== 'prod',
      lifecycleRules: [
        { description: 'Expire untagged after 7d', tagStatus: ecr.TagStatus.UNTAGGED, maxImageAge: cdk.Duration.days(7) },
        { description: 'Keep last 10', maxImageCount: 10 },
      ],
    });

    // ── S3 Assets Bucket ────────────────────────────────────────────
    const assetsAccessLogsBucket = new s3.Bucket(this, 'AssetsAccessLogsBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: stage !== 'prod',
      lifecycleRules: [{ id: 'ExpireLogs', expiration: cdk.Duration.days(90) }],
    });

    const assetsBucket = new s3.Bucket(this, 'AssetsBucket', {
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: stage !== 'prod',
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      serverAccessLogsBucket: assetsAccessLogsBucket,
      serverAccessLogsPrefix: 'assets/',
    });

    // Upload agent source code
    const sourceDeployment = new s3deploy.BucketDeployment(this, 'AgentSourceDeploy', {
      sources: [s3deploy.Source.asset('./agent')],
      destinationBucket: assetsBucket,
      destinationKeyPrefix: 'agent/',
    });

    // ── CodeBuild (ARM64 Docker) ────────────────────────────────────
    const buildEncryptionKey = new kms.Key(this, 'BuildEncryptionKey', {
      description: 'KMS key for CodeBuild project encryption',
      enableKeyRotation: true,
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    const buildProject = new codebuild.Project(this, 'AgentBuild', {
      projectName: `${prefix}-agent-build-${stage}`,
      encryptionKey: buildEncryptionKey,
      environment: {
        buildImage: codebuild.LinuxBuildImage.AMAZON_LINUX_2_ARM_3,
        computeType: codebuild.ComputeType.SMALL,
        privileged: true,
      },
      environmentVariables: {
        ECR_REPO_URI: { value: ecrRepo.repositoryUri },
        AWS_ACCOUNT_ID: { value: account },
        AWS_DEFAULT_REGION: { value: region },
        ASSETS_BUCKET: { value: assetsBucket.bucketName },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          pre_build: {
            commands: [
              'aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com',
              'aws s3 sync s3://$ASSETS_BUCKET/agent/ ./agent-src/',
            ],
          },
          build: {
            commands: [
              'cd agent-src',
              'docker build --no-cache -t $ECR_REPO_URI:latest .',
              'docker tag $ECR_REPO_URI:latest $ECR_REPO_URI:production',
            ],
          },
          post_build: {
            commands: [
              'docker push $ECR_REPO_URI:latest',
              'docker push $ECR_REPO_URI:production',
            ],
          },
        },
      }),
      timeout: cdk.Duration.minutes(20),
    });
    ecrRepo.grantPullPush(buildProject);
    assetsBucket.grantRead(buildProject);

    // ── Runtime Execution Role ──────────────────────────────────────
    const runtimeRole = new iam.Role(this, 'RuntimeRole', {
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        new iam.ServicePrincipal('bedrock.amazonaws.com'),
      ),
    });

    // Bedrock model invocation
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        `arn:aws:bedrock:*::foundation-model/*`,
        `arn:aws:bedrock:*:${account}:inference-profile/*`,
      ],
    }));

    // ECR pull
    ecrRepo.grantPull(runtimeRole);
    // ecr:GetAuthorizationToken is a service-level action that requires Resource: '*'
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ecr:GetAuthorizationToken'],
      resources: ['*'],
    }));

    // Gateway invocation
    if (props.gatewayArn) {
      runtimeRole.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [props.gatewayArn],
      }));
    }

    // Code Interpreter
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CodeInterpreter',
      actions: [
        'bedrock-agentcore:CreateCodeInterpreter', 'bedrock-agentcore:DeleteCodeInterpreter',
        'bedrock-agentcore:ListCodeInterpreters', 'bedrock-agentcore:GetCodeInterpreter',
        'bedrock-agentcore:StartCodeInterpreterSession', 'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession', 'bedrock-agentcore:GetCodeInterpreterSession',
        'bedrock-agentcore:ListCodeInterpreterSessions',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:*:*:code-interpreter/*`,
        `arn:aws:bedrock-agentcore:*:*:code-interpreter-custom/*`,
      ],
    }));

    // AgentCore Memory (short-term conversation history)
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AgentCoreMemory',
      actions: [
        'bedrock-agentcore:GetMemory',
        'bedrock-agentcore:CreateSession', 'bedrock-agentcore:GetSession',
        'bedrock-agentcore:ListSessions', 'bedrock-agentcore:DeleteSession',
        'bedrock-agentcore:CreateEvent', 'bedrock-agentcore:GetEvent',
        'bedrock-agentcore:ListEvents',
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:memory/*`],
    }));

    // PassRole for built-in tools
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [runtimeRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'bedrock-agentcore.amazonaws.com' } },
    }));

    // Athena + AWS Glue + S3 (scoped to analytics resources)
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults',
        'athena:GetWorkGroup',
      ],
      resources: [`arn:aws:athena:${region}:${account}:workgroup/${props.analyticsResources.athenaWorkGroupName}`],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions',
      ],
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/${props.analyticsResources.glueDatabaseName}`,
        `arn:aws:glue:${region}:${account}:table/${props.analyticsResources.glueDatabaseName}/*`,
      ],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket', 's3:PutObject', 's3:GetBucketLocation'],
      resources: [
        props.analyticsResources.rawDataBucketArn,
        `${props.analyticsResources.rawDataBucketArn}/*`,
        `arn:aws:s3:::${props.analyticsResources.athenaResultsBucketName}`,
        `arn:aws:s3:::${props.analyticsResources.athenaResultsBucketName}/*`,
      ],
    }));

    // CloudWatch Logs + X-Ray
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`arn:aws:logs:${region}:${account}:*`],
    }));
    // X-Ray tracing actions are service-level and require Resource: '*'
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
      resources: ['*'],
    }));

    // ── Code Interpreter Execution Role (S3 read for large datasets) ──
    const codeInterpreterRole = new iam.Role(this, 'CodeInterpreterRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for custom Code Interpreter with S3 read access to Athena results',
    });
    codeInterpreterRole.addToPolicy(new iam.PolicyStatement({
      sid: 'S3ReadAthenaResults',
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [
        `arn:aws:s3:::${props.analyticsResources.athenaResultsBucketName}`,
        `arn:aws:s3:::${props.analyticsResources.athenaResultsBucketName}/*`,
      ],
    }));

    // The deployer needs to pass this role to the Code Interpreter
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [codeInterpreterRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'bedrock-agentcore.amazonaws.com' } },
    }));

    // ── Custom Code Interpreter (S3-enabled sandbox) ────────────────
    const customCodeInterpreter = new agentcore.CfnCodeInterpreterCustom(this, 'CodeInterpreter', {
      name: `${prefix.replace(/-/g, '_')}_code_interpreter_${stage}`,
      description: 'Custom Code Interpreter with S3 read access for large dataset analysis',
      executionRoleArn: codeInterpreterRole.roleArn,
      networkConfiguration: { networkMode: 'SANDBOX' },
    });
    customCodeInterpreter.node.addDependency(codeInterpreterRole);

    // ── AgentCore Memory (short-term conversation history) ──────────
    const memory = new agentcore.CfnMemory(this, 'Memory', {
      name: `${prefix.replace(/-/g, '_')}_memory_${stage}`,
      description: 'Short-term conversation memory for SES analytics agent',
      eventExpiryDuration: 3, // 3 days (minimum allowed)
    });

    // ── Bedrock Guardrail (content moderation + PII redaction) ──────
    const guardrail = new bedrock.CfnGuardrail(this, 'Guardrail', {
      name: `${prefix.replace(/-/g, '_')}_guardrail_${stage}`,
      description: 'Content moderation guardrail for SES analytics agent',
      blockedInputMessaging: 'Your request could not be processed. Please rephrase your question about SES analytics.',
      blockedOutputsMessaging: 'The response was filtered for safety. Please try a different query.',

      // Content filters — block harmful content on both input and output
      contentPolicyConfig: {
        filtersConfig: [
          { type: 'SEXUAL', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'VIOLENCE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'HATE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'INSULTS', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'MISCONDUCT', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'PROMPT_ATTACK', inputStrength: 'HIGH', outputStrength: 'NONE' },
        ],
      },

      // Sensitive information — ANONYMIZE output PII (no blocking)
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          { type: 'EMAIL', action: 'ANONYMIZE' },
          { type: 'PHONE', action: 'ANONYMIZE' },
          { type: 'NAME', action: 'ANONYMIZE' },
          { type: 'US_SOCIAL_SECURITY_NUMBER', action: 'ANONYMIZE' },
          { type: 'CREDIT_DEBIT_CARD_NUMBER', action: 'ANONYMIZE' },
          { type: 'IP_ADDRESS', action: 'ANONYMIZE' },
        ],
      },

      // Word filters — block profanity
      wordPolicyConfig: {
        managedWordListsConfig: [{ type: 'PROFANITY' }],
      },
    });

    const guardrailVersion = new bedrock.CfnGuardrailVersion(this, 'GuardrailVersion', {
      guardrailIdentifier: guardrail.attrGuardrailId,
      description: `v${Date.now()}`,
    });

    // Grant the runtime role permission to apply the guardrail
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ApplyGuardrail',
      actions: ['bedrock:ApplyGuardrail'],
      resources: [guardrail.attrGuardrailArn],
    }));

    // ── Deployer Lambda ─────────────────────────────────────────────
    const deployerFn = new lambda.Function(this, 'DeployerFn', {
      functionName: `${prefix}-deployer-${stage}`,
      runtime: lambda.Runtime.PYTHON_3_11,
      architecture: lambda.Architecture.ARM_64,
      handler: 'index.handler',
      code: lambda.Code.fromAsset('lambda/deployer'),
      memorySize: 512,
      timeout: cdk.Duration.minutes(15),
      layers: [props.boto3Layer],
    });

    deployerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
      resources: [buildProject.projectArn],
    }));
    // AgentCore runtime management — scoped to runtime resources in this account
    deployerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:CreateAgentRuntime', 'bedrock-agentcore:UpdateAgentRuntime',
        'bedrock-agentcore:DeleteAgentRuntime', 'bedrock-agentcore:GetAgentRuntime',
        'bedrock-agentcore:ListAgentRuntimes',
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:runtime/*`],
    }));
    // iam:CreateServiceLinkedRole — scoped to AgentCore service principal
    deployerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:CreateServiceLinkedRole'],
      resources: ['*'],
      conditions: { StringEquals: { 'iam:AWSServiceName': 'bedrock-agentcore.amazonaws.com' } },
    }));
    // ecr:GetAuthorizationToken is a service-level action that requires Resource: '*'
    deployerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ecr:GetAuthorizationToken'],
      resources: ['*'],
    }));
    // ECR image operations — scoped to the agent repository
    deployerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ecr:BatchCheckLayerAvailability',
        'ecr:GetDownloadUrlForLayer', 'ecr:BatchGetImage',
      ],
      resources: [ecrRepo.repositoryArn],
    }));
    deployerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [runtimeRole.roleArn],
    }));
    ecrRepo.grantPull(deployerFn);

    // ── Custom Resource ─────────────────────────────────────────────
    const runtimeDeployment = new cdk.CustomResource(this, 'RuntimeDeployment', {
      serviceToken: deployerFn.functionArn,
      properties: {
        AgentName: agentName,
        BuildProject: buildProject.projectName,
        ExecutionRoleArn: runtimeRole.roleArn,
        EcrRepositoryUri: ecrRepo.repositoryUri,
        Region: region,
        GatewayUrl: props.gatewayUrl,
        ModelId: config.modelId,
        // Optional Amazon Bedrock Guardrail for content filtering
        ...(config.guardrailId && { GuardrailId: config.guardrailId }),
        ...(config.guardrailId && { GuardrailVersion: config.guardrailVersion ?? 'DRAFT' }),
        // Analytics resource refs for agent env vars
        DatabaseName: props.analyticsResources.glueDatabaseName,
        AthenaWorkGroup: props.analyticsResources.athenaWorkGroupName,
        AthenaResultsBucket: props.analyticsResources.athenaResultsBucketName,
        CodeInterpreterId: customCodeInterpreter.attrCodeInterpreterId,
        MemoryId: memory.attrMemoryId,
        GuardrailId: guardrail.attrGuardrailId,
        GuardrailVersion: guardrailVersion.attrVersion,
        SourceHash: Date.now().toString(),
      },
    });
    runtimeDeployment.node.addDependency(buildProject);
    runtimeDeployment.node.addDependency(runtimeRole);
    runtimeDeployment.node.addDependency(sourceDeployment);
    runtimeDeployment.node.addDependency(customCodeInterpreter);
    runtimeDeployment.node.addDependency(memory);
    runtimeDeployment.node.addDependency(guardrailVersion);

    this.runtimeArn = runtimeDeployment.getAttString('RuntimeArn');
    this.apiEndpoint = runtimeDeployment.getAttString('ApiEndpoint');

    new cdk.CfnOutput(this, 'RuntimeArn', { value: this.runtimeArn, description: 'AgentCore Runtime ARN' });
    new cdk.CfnOutput(this, 'ApiEndpoint', { value: this.apiEndpoint, description: 'AgentCore Runtime API Endpoint' });

    // ── cdk-nag suppressions ────────────────────────────────────────
    const stack = cdk.Stack.of(this);

    // IAM4: AWSLambdaBasicExecutionRole is the standard CDK-managed policy for Lambda log access
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Runtime/DeployerFn/ServiceRole/Resource`,
    ], [{ id: 'AwsSolutions-IAM4', reason: 'AWSLambdaBasicExecutionRole is the standard CDK-managed policy for Lambda CloudWatch Logs access.' }]);

    // L1: Deployer Lambda uses Python 3.11 to match the shared boto3 layer runtime
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Runtime/DeployerFn/Resource`,
    ], [{ id: 'AwsSolutions-L1', reason: 'Deployer Lambda uses Python 3.11 to match the shared boto3 layer runtime compatibility.' }]);

    // IAM5: Wildcard permissions are either resource-scoped (bucket/*, runtime/*, etc.)
    // or required by AWS APIs that mandate Resource: '*' (ecr:GetAuthorizationToken, xray:Put*)
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Runtime/RuntimeRole/DefaultPolicy/Resource`,
      `${stack.stackName}/Runtime/CodeInterpreterRole/DefaultPolicy/Resource`,
      `${stack.stackName}/Runtime/AgentBuild/Role/DefaultPolicy/Resource`,
      `${stack.stackName}/Runtime/DeployerFn/ServiceRole/DefaultPolicy/Resource`,
    ], [{
      id: 'AwsSolutions-IAM5',
      reason: 'Wildcard permissions are either resource-scoped (bucket/*, runtime/*, table/db/*, foundation-model/*, code-interpreter/*) or required by AWS APIs that mandate Resource: * (ecr:GetAuthorizationToken, xray:PutTraceSegments, iam:CreateServiceLinkedRole with condition). S3 action wildcards (s3:GetObject*, s3:List*) are CDK grant() defaults.',
    }], true);

    // S1: Access logs bucket itself does not need access logging (would create infinite loop)
    NagSuppressions.addResourceSuppressionsByPath(stack, [
      `${stack.stackName}/Runtime/AssetsAccessLogsBucket/Resource`,
    ], [{ id: 'AwsSolutions-S1', reason: 'This is the access logs destination bucket — enabling access logging on it would create an infinite loop.' }]);
  }
}
