# Copyright Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT
"""S3 event-triggered Lambda to add Amazon Athena partitions when new data arrives."""
import json
import os
import re
import time
import logging
from urllib.parse import unquote
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DB = os.environ['DATABASE_NAME']
TABLE = os.environ['TABLE_NAME']
OUTPUT = os.environ['OUTPUT_LOCATION']
WG = os.environ['WORKGROUP']

athena = boto3.client('athena')

# Regex to extract Hive-style partition values from S3 key
PARTITION_RE = re.compile(
    r'events/year=(\d{4})/month=(\d{2})/day=(\d{2})/hour=(\d{2})/'
)


def handler(event, context):
    """Process S3 event notifications and add partitions."""
    partitions_added = set()

    # Log the event structure for debugging
    records = event.get('Records', [])
    logger.info(f"Received {len(records)} records")
    if records:
        logger.info(f"First record key: {records[0].get('s3', {}).get('object', {}).get('key', 'N/A')}")

    for record in records:
        key = record.get('s3', {}).get('object', {}).get('key', '')
        # S3 event notifications URL-encode the key — decode %3D back to =
        key = unquote(key)
        m = PARTITION_RE.search(key)
        if not m:
            logger.info(f"Key did not match partition pattern: {key[:200]}")
            continue

        year, month, day, hour = m.groups()
        partition_key = f"{year}/{month}/{day}/{hour}"

        if partition_key in partitions_added:
            continue

        bucket = record['s3']['bucket']['name']
        location = f"s3://{bucket}/events/year={year}/month={month}/day={day}/hour={hour}/"

        ddl = (
            f"ALTER TABLE {DB}.{TABLE} ADD IF NOT EXISTS "
            f"PARTITION (year='{year}', month='{month}', day='{day}', hour='{hour}') "
            f"LOCATION '{location}'"
        )

        try:
            resp = athena.start_query_execution(
                QueryString=ddl,
                QueryExecutionContext={'Database': DB},
                ResultConfiguration={'OutputLocation': OUTPUT},
                WorkGroup=WG,
            )
            wait_for_query(resp['QueryExecutionId'])
            partitions_added.add(partition_key)
            logger.info(f"Partition added: {partition_key}")
        except Exception as e:
            logger.error(f"Failed to add partition {partition_key}: {e}")

    logger.info(f"Processed {len(partitions_added)} partitions")

    # Fallback: if no partitions were extracted from the event, scan S3
    # for partition paths and add them directly via ALTER TABLE.
    if not partitions_added:
        try:
            logger.info("No partitions from event — scanning S3 for partitions")
            s3 = boto3.client('s3')
            bucket = ''
            if records:
                bucket = records[0].get('s3', {}).get('bucket', {}).get('name', '')

            if bucket:
                seen = set()
                paginator = s3.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=bucket, Prefix='events/year='):
                    for obj in page.get('Contents', []):
                        m = PARTITION_RE.search(obj['Key'])
                        if not m:
                            continue
                        year, month, day, hour = m.groups()
                        pk = f"{year}/{month}/{day}/{hour}"
                        if pk in seen:
                            continue
                        seen.add(pk)
                        location = f"s3://{bucket}/events/year={year}/month={month}/day={day}/hour={hour}/"
                        ddl = (
                            f"ALTER TABLE {DB}.{TABLE} ADD IF NOT EXISTS "
                            f"PARTITION (year='{year}', month='{month}', day='{day}', hour='{hour}') "
                            f"LOCATION '{location}'"
                        )
                        resp = athena.start_query_execution(
                            QueryString=ddl,
                            QueryExecutionContext={'Database': DB},
                            ResultConfiguration={'OutputLocation': OUTPUT},
                            WorkGroup=WG,
                        )
                        wait_for_query(resp['QueryExecutionId'], timeout=120)
                        logger.info(f"Fallback partition added: {pk}")
            else:
                logger.warning("No bucket name available for fallback scan")
        except Exception as e:
            logger.error(f"Fallback partition scan failed: {e}")

    return {'partitions_added': len(partitions_added)}


def wait_for_query(qid, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        r = athena.get_query_execution(QueryExecutionId=qid)
        state = r['QueryExecution']['Status']['State']
        if state == 'SUCCEEDED':
            return
        if state in ('FAILED', 'CANCELLED'):
            reason = r['QueryExecution']['Status'].get('StateChangeReason', '')
            # "Partition already exists" is fine
            if 'already exists' in reason.lower():
                return
            raise RuntimeError(f"Query {state}: {reason}")
        time.sleep(1)
