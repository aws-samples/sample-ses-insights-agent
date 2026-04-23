# Copyright Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT
"""Custom resource Lambda to create the Amazon SES events AWS Glue/Amazon Athena table."""
import json
import os
import time
import logging
import boto3
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")
    if event['RequestType'] == 'Delete':
        return send(event, 'SUCCESS', {})

    db = os.environ['DATABASE_NAME']
    table = os.environ['TABLE_NAME']
    s3_loc = os.environ['S3_LOCATION']
    output = os.environ['OUTPUT_LOCATION']
    wg = os.environ['WORKGROUP']

    ddl = f"""
CREATE EXTERNAL TABLE IF NOT EXISTS {db}.{table} (
  eventType string,
  mail struct<
    timestamp: string,
    source: string,
    sourceArn: string,
    sendingAccountId: string,
    messageId: string,
    destination: array<string>,
    headersTruncated: boolean,
    headers: array<struct<name: string, value: string>>,
    commonHeaders: struct<`from`: array<string>, `to`: array<string>, messageId: string, subject: string>
  >,
  send map<string,string>,
  delivery struct<
    timestamp: string,
    processingTimeMillis: bigint,
    recipients: array<string>,
    smtpResponse: string,
    reportingMTA: string
  >,
  open struct<
    ipAddress: string,
    timestamp: string,
    userAgent: string
  >,
  click struct<
    ipAddress: string,
    link: string,
    linkTags: map<string,array<string>>,
    timestamp: string,
    userAgent: string
  >,
  bounce struct<
    bounceType: string,
    bounceSubType: string,
    bouncedRecipients: array<struct<
      emailAddress: string,
      action: string,
      status: string,
      diagnosticCode: string
    >>,
    timestamp: string,
    feedbackId: string,
    reportingMTA: string
  >,
  complaint struct<
    complainedRecipients: array<struct<emailAddress: string>>,
    timestamp: string,
    feedbackId: string,
    userAgent: string,
    complaintFeedbackType: string,
    arrivalDate: string
  >,
  reject struct<reason: string>,
  renderingFailure struct<errorMessage: string, templateName: string>
)
PARTITIONED BY (year string, month string, day string, hour string)
STORED AS parquet
LOCATION '{s3_loc}'
TBLPROPERTIES ("parquet.compression"="SNAPPY")
"""

    athena = boto3.client('athena')
    try:
        resp = athena.start_query_execution(
            QueryString=ddl,
            QueryExecutionContext={'Database': db},
            ResultConfiguration={'OutputLocation': output},
            WorkGroup=wg,
        )
        qid = resp['QueryExecutionId']
        wait_for_query(athena, qid)
        logger.info(f"Table {db}.{table} created successfully")
    except Exception as e:
        logger.error(f"Table creation error: {e}")

    return send(event, 'SUCCESS', {'TableName': table})


def wait_for_query(client, qid, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        r = client.get_query_execution(QueryExecutionId=qid)
        state = r['QueryExecution']['Status']['State']
        if state == 'SUCCEEDED':
            return
        if state in ('FAILED', 'CANCELLED'):
            reason = r['QueryExecution']['Status'].get('StateChangeReason', '')
            raise RuntimeError(f"Query {state}: {reason}")
        time.sleep(2)
    raise TimeoutError("Query timed out")


def send(event, status, data):
    body = json.dumps({
        'Status': status,
        'PhysicalResourceId': data.get('TableName', 'ses-events-table'),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data,
    })
    http = urllib3.PoolManager()
    http.request('PUT', event['ResponseURL'], body=body.encode('utf-8'),
                 headers={'Content-Type': 'application/json'})
    return {'Status': status, 'Data': data}
