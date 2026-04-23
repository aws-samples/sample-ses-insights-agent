# Copyright Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT
"""Gateway tool: Describe the Amazon Simple Email Service (Amazon SES) analytics database schema."""
import json
import os
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DB = os.environ['DATABASE_NAME']
glue = boto3.client('glue')


def lambda_handler(event, context):
    """Return schema information for the SES analytics database."""
    logger.info(f"Event: {json.dumps(event)}")
    table_name = event.get('table_name', '')

    try:
        if table_name:
            return describe_table(table_name)
        return describe_all_tables()
    except Exception as e:
        logger.error(f"Schema error: {e}")
        return {'error': str(e)}


def describe_all_tables():
    """Describe the ses_events table and list available views."""
    # Always describe the main ses_events table in detail
    try:
        main_table = describe_table('ses_events')
    except Exception:
        main_table = {'error': 'ses_events table not found'}

    # List other tables/views by name only (no full schema dump)
    resp = glue.get_tables(DatabaseName=DB)
    views = [t['Name'] for t in resp['TableList'] if t['Name'] != 'ses_events']

    return {
        'database': DB,
        'main_table': main_table,
        'available_views': views,
        'hint': 'Use query_ses_analytics to run SQL queries. '
                'The ses_events table contains all SES event data partitioned by year/month/day/hour. '
                'Views (daily_summary, bounce_analysis, sender_reputation, hourly_volume) '
                'provide pre-aggregated data. Use describe_ses_schema with a specific table_name to see view columns.',
    }


def describe_table(table_name):
    """Describe a specific table in detail."""
    resp = glue.get_table(DatabaseName=DB, Name=table_name)
    t = resp['Table']

    cols = []
    for c in t.get('StorageDescriptor', {}).get('Columns', []):
        cols.append({
            'name': c['Name'],
            'type': c['Type'],
            'comment': c.get('Comment', ''),
        })

    partitions = [{'name': p['Name'], 'type': p['Type']} for p in t.get('PartitionKeys', [])]

    # Get sample partition info
    try:
        parts_resp = glue.get_partitions(DatabaseName=DB, TableName=table_name, MaxResults=5)
        sample_partitions = [p['Values'] for p in parts_resp.get('Partitions', [])]
    except Exception:
        sample_partitions = []

    return {
        'database': DB,
        'table_name': table_name,
        'description': t.get('Description', ''),
        'columns': cols,
        'partition_keys': partitions,
        'sample_partitions': sample_partitions,
        'location': t.get('StorageDescriptor', {}).get('Location', ''),
        'format': t.get('StorageDescriptor', {}).get('InputFormat', ''),
        'serde': t.get('StorageDescriptor', {}).get('SerdeInfo', {}).get('SerializationLibrary', ''),
        'row_count_hint': 'Use query_ses_analytics with SELECT COUNT(*) to get exact counts',
    }
