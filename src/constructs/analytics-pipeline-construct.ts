import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3Notifications from 'aws-cdk-lib/aws-s3-notifications';
import * as firehose from 'aws-cdk-lib/aws-kinesisfirehose';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ses from 'aws-cdk-lib/aws-ses';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { Construct } from 'constructs';
import { NagSuppressions } from 'cdk-nag';
import * as path from 'path';

export interface AnalyticsPipelineProps {
  readonly stage: string;
  readonly sesConfigurationSetName: string;
  /** Override Firehose buffer interval in seconds (1–900). Defaults to 60 for dev, 300 for prod. */
  readonly firehoseBufferIntervalSeconds?: number;
  /** Override Firehose buffer size in MB (1–128). Defaults to 64. */
  readonly firehoseBufferSizeMBs?: number;
}

/**
 * Amazon Simple Email Service (Amazon SES) Analytics Pipeline:
 * Amazon SES → Amazon Data Firehose → Amazon S3 (Parquet) → AWS Glue → Amazon Athena
 *
 * No campaign tags — pure event-level analytics (sends, deliveries, bounces,
 * complaints, opens, clicks). Cost-optimized with Parquet + SNAPPY compression,
 * lifecycle policies, and Athena scan limits.
 */
export class AnalyticsPipelineConstruct extends Construct {
  public readonly rawDataBucket: s3.Bucket;
  public readonly athenaResultsBucket: s3.Bucket;
  public readonly glueDatabaseName: string;
  public readonly athenaWorkGroupName: string;
  private readonly firehoseBufferIntervalSeconds?: number;
  private readonly firehoseBufferSizeMBs?: number;

  constructor(scope: Construct, id: string, props: AnalyticsPipelineProps) {
    super(scope, id);

    const stage = props.stage;
    this.glueDatabaseName = `ses_analytics_${stage}`;
    this.firehoseBufferIntervalSeconds = props.firehoseBufferIntervalSeconds;
    this.firehoseBufferSizeMBs = props.firehoseBufferSizeMBs;

    // ── S3 Buckets ──────────────────────────────────────────────────
    // Access logs bucket for auditing data access on analytics buckets
    const accessLogsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: stage !== 'prod',
      lifecycleRules: [{ id: 'ExpireLogs', expiration: cdk.Duration.days(90) }],
    });

    this.rawDataBucket = new s3.Bucket(this, 'RawDataBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: stage !== 'prod',
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: 'raw-data/',
      lifecycleRules: [
        { id: 'ToIA', transitions: [{ storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(90) }] },
        { id: 'Expire', expiration: cdk.Duration.days(365) },
      ],
    });

    this.athenaResultsBucket = new s3.Bucket(this, 'AthenaResultsBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: stage === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: stage !== 'prod',
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: 'athena-results/',
      lifecycleRules: [{ id: 'ExpireResults', expiration: cdk.Duration.days(30) }],
    });

    // ── Glue Database ───────────────────────────────────────────────
    const glueDb = new glue.CfnDatabase(this, 'GlueDatabase', {
      catalogId: cdk.Stack.of(this).account,
      databaseInput: {
        name: this.glueDatabaseName,
        description: 'SES event analytics database',
      },
    });

    // ── Athena Workgroup ────────────────────────────────────────────
    // Use a unique suffix to avoid naming conflicts during rollbacks
    const uniqueSuffix = cdk.Names.uniqueId(this).slice(-8).toLowerCase();
    this.athenaWorkGroupName = `ses-analytics-wg-${stage}-${uniqueSuffix}`;

    const workGroup = new athena.CfnWorkGroup(this, 'AthenaWorkGroup', {
      name: this.athenaWorkGroupName,
      description: 'SES analytics queries',
      recursiveDeleteOption: true,
      workGroupConfiguration: {
        resultConfiguration: {
          outputLocation: `s3://${this.athenaResultsBucket.bucketName}/query-results/`,
          encryptionConfiguration: { encryptionOption: 'SSE_S3' },
        },
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
        bytesScannedCutoffPerQuery: 10 * 1024 * 1024 * 1024, // 10 GB
        engineVersion: { selectedEngineVersion: 'Athena engine version 3' },
      },
    });

    // ── Glue Table + Firehose + SES Event Destination ───────────────
    const rawTableName = 'ses_events';
    const tableCreation = this.createSesEventsTable(rawTableName, glueDb);
    const deliveryStream = this.createFirehose(rawTableName, glueDb, tableCreation, stage);
    this.createPartitionLambda(rawTableName, glueDb, stage);
    this.createScheduledMaintenance(rawTableName, glueDb, stage);
    this.createSesEventDestination(deliveryStream, props.sesConfigurationSetName, stage);

    // ── cdk-nag suppressions ────────────────────────────────────────
    // IAM4: AWSLambdaBasicExecutionRole is the standard CDK-managed policy for Lambda log access
    NagSuppressions.addResourceSuppressionsByPath(cdk.Stack.of(this), [
      `${cdk.Stack.of(this).stackName}/Analytics/CreateTableFn/ServiceRole/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/PartitionMgrFn/ServiceRole/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/MaintenanceFn/ServiceRole/Resource`,
    ], [{ id: 'AwsSolutions-IAM4', reason: 'AWSLambdaBasicExecutionRole is the standard CDK-managed policy for Lambda CloudWatch Logs access.' }]);

    // L1: Lambda functions use Python 3.11 to match the shared boto3 layer runtime
    NagSuppressions.addResourceSuppressionsByPath(cdk.Stack.of(this), [
      `${cdk.Stack.of(this).stackName}/Analytics/CreateTableFn/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/PartitionMgrFn/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/MaintenanceFn/Resource`,
    ], [{ id: 'AwsSolutions-L1', reason: 'Lambda functions use Python 3.11 to match the shared boto3 layer runtime compatibility.' }]);

    // IAM5: Wildcard permissions are required for S3 object-level access (bucket/*),
    // Glue table-level access (database/*), and Firehose S3 delivery operations
    NagSuppressions.addResourceSuppressionsByPath(cdk.Stack.of(this), [
      `${cdk.Stack.of(this).stackName}/Analytics/FirehoseRole/DefaultPolicy/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/CreateTableFn/ServiceRole/DefaultPolicy/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/PartitionMgrFn/ServiceRole/DefaultPolicy/Resource`,
      `${cdk.Stack.of(this).stackName}/Analytics/MaintenanceFn/ServiceRole/DefaultPolicy/Resource`,
    ], [{
      id: 'AwsSolutions-IAM5',
      reason: 'Wildcard permissions are resource-scoped (bucket/*, database/table/*) and required for S3 object operations, Glue table access, and Firehose delivery. Actions like s3:DeleteObject* and s3:Abort* are needed for S3 lifecycle and multipart upload management.',
    }], true);
  }

  // ── Glue Table (Parquet, Hive-partitioned) ──────────────────────
  private createSesEventsTable(tableName: string, glueDb: glue.CfnDatabase): cdk.CustomResource {
    const createTableLambda = new lambda.Function(this, 'CreateTableFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'create_table.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/analytics')),
      timeout: cdk.Duration.minutes(5),
      environment: {
        DATABASE_NAME: this.glueDatabaseName,
        TABLE_NAME: tableName,
        S3_LOCATION: `s3://${this.rawDataBucket.bucketName}/events/`,
        OUTPUT_LOCATION: `s3://${this.athenaResultsBucket.bucketName}/table-creation/`,
        WORKGROUP: this.athenaWorkGroupName,
      },
    });

    this.athenaResultsBucket.grantReadWrite(createTableLambda);
    this.rawDataBucket.grantRead(createTableLambda);
    this.addAthenaGluePermissions(createTableLambda);

    const trigger = new cdk.CustomResource(this, 'TableCreation', {
      serviceToken: createTableLambda.functionArn,
    });
    trigger.node.addDependency(glueDb);
    return trigger;
  }

  // ── Firehose (DirectPut → S3 Parquet) ───────────────────────────
  private createFirehose(
    tableName: string,
    glueDb: glue.CfnDatabase,
    tableCreation: cdk.CustomResource,
    stage: string,
  ): firehose.CfnDeliveryStream {
    const role = new iam.Role(this, 'FirehoseRole', {
      assumedBy: new iam.ServicePrincipal('firehose.amazonaws.com'),
    });
    this.rawDataBucket.grantWrite(role);

    const logGroup = new logs.LogGroup(this, 'FirehoseLogs', {
      logGroupName: `/aws/kinesisfirehose/ses-analytics-${stage}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const logStream = new logs.LogStream(this, 'FirehoseLogStream', { logGroup, logStreamName: 'S3Delivery' });
    logGroup.grantWrite(role);

    // AWS Glue permissions for Parquet conversion — scoped to the ses_events table only
    const gluePolicy = new iam.Policy(this, 'FirehoseGluePolicy', {
      statements: [new iam.PolicyStatement({
        actions: ['glue:GetTable', 'glue:GetTableVersion', 'glue:GetTableVersions'],
        resources: [
          `arn:aws:glue:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:catalog`,
          `arn:aws:glue:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:database/${this.glueDatabaseName}`,
          `arn:aws:glue:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:table/${this.glueDatabaseName}/${tableName}`,
        ],
      })],
    });
    gluePolicy.attachToRole(role);

    const errorLogStream = new logs.LogStream(this, 'FirehoseErrorLogStream', { logGroup, logStreamName: 'ErrorDelivery' });

    // Buffer interval: configurable, defaults to 300s for production (fewer, larger Parquet files
    // = better Athena perf), 60s for dev (faster feedback loop)
    const bufferInterval = this.firehoseBufferIntervalSeconds
      ?? (stage === 'prod' ? 300 : 60);
    const bufferSize = this.firehoseBufferSizeMBs ?? 64;

    const stream = new firehose.CfnDeliveryStream(this, 'DeliveryStream', {
      deliveryStreamName: `ses-analytics-stream-${stage}`,
      deliveryStreamType: 'DirectPut',
      deliveryStreamEncryptionConfigurationInput: {
        keyType: 'AWS_OWNED_CMK',
      },
      extendedS3DestinationConfiguration: {
        bucketArn: this.rawDataBucket.bucketArn,
        roleArn: role.roleArn,
        prefix: 'events/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/',
        errorOutputPrefix: 'errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/',
        bufferingHints: { sizeInMBs: bufferSize, intervalInSeconds: bufferInterval },
        compressionFormat: 'UNCOMPRESSED', // Required for Parquet conversion
        dataFormatConversionConfiguration: {
          enabled: true,
          schemaConfiguration: {
            databaseName: this.glueDatabaseName,
            tableName,
            region: cdk.Stack.of(this).region,
            roleArn: role.roleArn,
          },
          inputFormatConfiguration: { deserializer: { openXJsonSerDe: {} } },
          outputFormatConfiguration: { serializer: { parquetSerDe: { compression: 'SNAPPY' } } },
        },
        cloudWatchLoggingOptions: {
          enabled: true,
          logGroupName: logGroup.logGroupName,
          logStreamName: logStream.logStreamName,
        },
        // Backup failed records so Parquet conversion failures aren't silently lost
        s3BackupMode: 'Enabled',
        s3BackupConfiguration: {
          bucketArn: this.rawDataBucket.bucketArn,
          roleArn: role.roleArn,
          prefix: 'backup/failed-records/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/',
          errorOutputPrefix: 'backup/errors/!{firehose:error-output-type}/',
          bufferingHints: { sizeInMBs: 5, intervalInSeconds: 300 },
          compressionFormat: 'GZIP',
          cloudWatchLoggingOptions: {
            enabled: true,
            logGroupName: logGroup.logGroupName,
            logStreamName: errorLogStream.logStreamName,
          },
        },
      },
    });

    stream.node.addDependency(gluePolicy);
    stream.node.addDependency(tableCreation);
    return stream;
  }

  // ── Partition Manager Lambda (S3 event trigger) ─────────────────
  private createPartitionLambda(tableName: string, glueDb: glue.CfnDatabase, stage: string): void {
    const fn = new lambda.Function(this, 'PartitionMgrFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'partition_manager.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/analytics')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      environment: {
        DATABASE_NAME: this.glueDatabaseName,
        TABLE_NAME: tableName,
        OUTPUT_LOCATION: `s3://${this.athenaResultsBucket.bucketName}/partition-mgmt/`,
        WORKGROUP: this.athenaWorkGroupName,
      },
    });

    this.rawDataBucket.grantRead(fn);
    this.athenaResultsBucket.grantReadWrite(fn);
    this.addAthenaGluePermissions(fn);

    fn.addPermission('S3Invoke', {
      principal: new iam.ServicePrincipal('s3.amazonaws.com'),
      sourceAccount: cdk.Stack.of(this).account,
      sourceArn: this.rawDataBucket.bucketArn,
    });

    this.rawDataBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3Notifications.LambdaDestination(fn),
      { prefix: 'events/' },
    );
  }

  // ── Scheduled Maintenance (daily MSCK REPAIR + views refresh) ────
  private createScheduledMaintenance(tableName: string, glueDb: glue.CfnDatabase, stage: string): void {
    const fn = new lambda.Function(this, 'MaintenanceFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'scheduled_maintenance.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/analytics')),
      timeout: cdk.Duration.minutes(10),
      memorySize: 256,
      environment: {
        DATABASE_NAME: this.glueDatabaseName,
        TABLE_NAME: tableName,
        OUTPUT_LOCATION: `s3://${this.athenaResultsBucket.bucketName}/maintenance/`,
        WORKGROUP: this.athenaWorkGroupName,
      },
    });

    this.rawDataBucket.grantRead(fn);
    this.athenaResultsBucket.grantReadWrite(fn);
    this.addAthenaGluePermissions(fn);

    // Run daily at 2 AM UTC
    new events.Rule(this, 'MaintenanceSchedule', {
      schedule: events.Schedule.cron({ minute: '0', hour: '2' }),
      targets: [new targets.LambdaFunction(fn)],
      description: 'Daily partition repair and analytics view refresh',
    });
  }

  // ── SES Event Destination → Firehose ────────────────────────────
  private createSesEventDestination(
    stream: firehose.CfnDeliveryStream,
    configSetName: string,
    stage: string,
  ): void {
    const role = new iam.Role(this, 'SesFirehoseRole', {
      assumedBy: new iam.ServicePrincipal('ses.amazonaws.com'),
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['firehose:PutRecord', 'firehose:PutRecordBatch'],
      resources: [stream.attrArn],
    }));

    const dest = new ses.CfnConfigurationSetEventDestination(this, 'SesEventDest', {
      configurationSetName: configSetName,
      eventDestination: {
        name: `firehose-dest-${stage}`,
        enabled: true,
        matchingEventTypes: ['send', 'reject', 'bounce', 'complaint', 'delivery', 'open', 'click', 'renderingFailure'],
        kinesisFirehoseDestination: {
          deliveryStreamArn: stream.attrArn,
          iamRoleArn: role.roleArn,
        },
      },
    });
    dest.node.addDependency(stream);
    dest.node.addDependency(role);
  }

  // ── Helper: Athena + Glue permissions ───────────────────────────
  private addAthenaGluePermissions(fn: lambda.Function): void {
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults',
      ],
      resources: [`arn:aws:athena:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:workgroup/${this.athenaWorkGroupName}`],
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'glue:GetDatabase', 'glue:GetTable', 'glue:CreateTable', 'glue:UpdateTable',
        'glue:CreatePartition', 'glue:BatchCreatePartition',
      ],
      resources: [
        `arn:aws:glue:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:catalog`,
        `arn:aws:glue:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:database/${this.glueDatabaseName}`,
        `arn:aws:glue:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:table/${this.glueDatabaseName}/*`,
      ],
    }));
  }
}
