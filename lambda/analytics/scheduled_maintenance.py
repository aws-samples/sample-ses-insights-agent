# Copyright Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT
"""Scheduled Lambda: MSCK REPAIR TABLE fallback + create/refresh Amazon Athena views.

Triggered daily by EventBridge to catch any missed partitions and keep
analytics views up to date.
"""
import json
import os
import time
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DB = os.environ['DATABASE_NAME']
TABLE = os.environ['TABLE_NAME']
OUTPUT = os.environ['OUTPUT_LOCATION']
WG = os.environ['WORKGROUP']

athena = boto3.client('athena')

# Pre-built analytics views
# Note: DB is an environment variable set by CDK at deploy time (not user input).
# SQL identifiers (database/table names) cannot be parameterized, so f-string
# interpolation is the correct approach here. This is not a SQL injection vector.
VIEWS = {
    'daily_summary': f"""
CREATE OR REPLACE VIEW {DB}.daily_summary AS
SELECT
  DATE(from_iso8601_timestamp(mail.timestamp)) as send_date,
  COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END) as sends,
  COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) as deliveries,
  COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) as bounces,
  COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) as complaints,
  COUNT(DISTINCT CASE WHEN eventtype = 'Open' THEN mail.messageid END) as opens,
  COUNT(DISTINCT CASE WHEN eventtype = 'Click' THEN mail.messageid END) as clicks,
  COUNT(DISTINCT CASE WHEN eventtype = 'Reject' THEN mail.messageid END) as rejects,
  COUNT(DISTINCT mail.source) as unique_senders,
  COUNT(DISTINCT element_at(mail.destination, 1)) as unique_recipients,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END), 0) * 100 as delivery_rate,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Open' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END), 0) * 100 as open_rate,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Click' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END), 0) * 100 as click_rate,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END), 0) * 100 as bounce_rate,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END), 0) * 100 as complaint_rate
FROM {DB}.ses_events
GROUP BY DATE(from_iso8601_timestamp(mail.timestamp))
""",

    'bounce_analysis': f"""
CREATE OR REPLACE VIEW {DB}.bounce_analysis AS
SELECT
  bounce.bouncetype as bounce_type,
  bounce.bouncesubtype as bounce_subtype,
  COUNT(DISTINCT mail.messageid) as bounce_count,
  ARRAY_AGG(DISTINCT element_at(bounce.bouncedrecipients, 1).emailaddress) as sample_recipients,
  MIN(mail.timestamp) as first_seen,
  MAX(mail.timestamp) as last_seen
FROM {DB}.ses_events
WHERE eventtype = 'Bounce' AND bounce IS NOT NULL
GROUP BY bounce.bouncetype, bounce.bouncesubtype
ORDER BY bounce_count DESC
""",

    'sender_reputation': f"""
CREATE OR REPLACE VIEW {DB}.sender_reputation AS
SELECT
  mail.source as sender,
  COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END) as sends,
  COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) as deliveries,
  COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) as bounces,
  COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) as complaints,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END), 0) * 100 as bounce_rate,
  CAST(COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) AS DOUBLE) /
    NULLIF(COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END), 0) * 100 as complaint_rate,
  MIN(mail.timestamp) as first_send,
  MAX(mail.timestamp) as last_send
FROM {DB}.ses_events
GROUP BY mail.source
ORDER BY sends DESC
""",

    'hourly_volume': f"""
CREATE OR REPLACE VIEW {DB}.hourly_volume AS
SELECT
  year, month, day, hour,
  COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END) as sends,
  COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) as deliveries,
  COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) as bounces
FROM {DB}.ses_events
GROUP BY year, month, day, hour
ORDER BY year DESC, month DESC, day DESC, hour DESC
""",
}


def handler(event, context):
    """Run MSCK REPAIR TABLE and refresh analytics views."""
    results = {'repair': None, 'views': {}}

    # 1. Repair partitions
    try:
        logger.info(f"Running MSCK REPAIR TABLE on {DB}.{TABLE}")
        qid = run_query(f"MSCK REPAIR TABLE {DB}.{TABLE}")
        results['repair'] = 'success'
        logger.info(f"Partition repair completed: {qid}")
    except Exception as e:
        results['repair'] = f'error: {e}'
        logger.error(f"Partition repair failed: {e}")

    # 2. Create/refresh views
    for view_name, view_sql in VIEWS.items():
        try:
            logger.info(f"Creating/refreshing view: {view_name}")
            qid = run_query(view_sql)
            results['views'][view_name] = 'success'
            logger.info(f"View {view_name} ready: {qid}")
        except Exception as e:
            results['views'][view_name] = f'error: {e}'
            logger.error(f"View {view_name} failed: {e}")

    logger.info(f"Maintenance complete: {json.dumps(results)}")
    return results


def run_query(sql, timeout=120):
    resp = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={'Database': DB},
        ResultConfiguration={'OutputLocation': OUTPUT},
        WorkGroup=WG,
    )
    qid = resp['QueryExecutionId']
    start = time.time()
    while time.time() - start < timeout:
        r = athena.get_query_execution(QueryExecutionId=qid)
        state = r['QueryExecution']['Status']['State']
        if state == 'SUCCEEDED':
            return qid
        if state in ('FAILED', 'CANCELLED'):
            reason = r['QueryExecution']['Status'].get('StateChangeReason', '')
            raise RuntimeError(f"Query {state}: {reason}")
        time.sleep(2)
    raise TimeoutError(f"Query timed out after {timeout}s")
