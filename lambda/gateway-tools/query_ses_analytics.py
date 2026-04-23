# Copyright Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT
"""Gateway tool: Execute Amazon Athena SQL queries against Amazon Simple Email Service (Amazon SES) analytics data.

Security: Only SELECT/SHOW/DESCRIBE/EXPLAIN/WITH queries are allowed.
Multi-statement queries (semicolons) are rejected. SQL comments are stripped.

Large result handling: When results exceed INLINE_ROW_LIMIT rows, the response
points to Athena's own CSV output in S3 (already written by the query engine)
so the Code Interpreter can download it directly — no extra pagination or
S3 writes needed.
"""
import json
import os
import re
import time
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DB = os.environ['DATABASE_NAME']
WG = os.environ['WORKGROUP']
OUTPUT = os.environ['OUTPUT_LOCATION']

athena = boto3.client('athena')

# Results at or below this limit are returned inline as JSON.
# Above this, we point to Athena's S3 output CSV.
INLINE_ROW_LIMIT = 500
PREVIEW_ROW_COUNT = 10

# Allowlist: only these statement types can execute
ALLOWED_PREFIXES = {'SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'WITH'}


def sanitize_sql(sql: str) -> str:
    """Strip comments and normalize whitespace."""
    # Remove single-line comments (-- ...)
    sql = re.sub(r'--[^\n]*', ' ', sql)
    # Remove multi-line comments (/* ... */)
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    # Collapse whitespace
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql


def validate_sql(sql: str) -> str | None:
    """Return an error message if the query is not safe, else None."""
    cleaned = sanitize_sql(sql)

    if not cleaned:
        return 'Empty query after sanitization'

    # Reject multi-statement queries (semicolons not inside string literals)
    # Simple heuristic: strip all quoted strings, then check for semicolons
    no_strings = re.sub(r"'(?:[^']|'')*'", '', cleaned)
    if ';' in no_strings:
        return 'Multi-statement queries are not allowed (semicolons detected)'

    # Check the first keyword is in the allowlist
    first_word = cleaned.split()[0].upper()
    if first_word not in ALLOWED_PREFIXES:
        return f'Only read queries are allowed. Got: {first_word}. Allowed: {", ".join(sorted(ALLOWED_PREFIXES))}'

    return None


def lambda_handler(event, context):
    """Execute an Athena query and return results as JSON."""
    logger.info(f"Event: {json.dumps(event)}")

    sql = event.get('sql_query', '').strip()
    if not sql:
        return {'error': 'sql_query is required'}

    # Allow forcing the S3 path for testing with small datasets
    force_s3 = event.get('force_s3', False)

    # Validate query safety
    error = validate_sql(sql)
    if error:
        logger.warning(f"Query rejected: {error} | SQL: {sql[:200]}")
        return {'error': error}

    try:
        resp = athena.start_query_execution(
            QueryString=sanitize_sql(sql),
            QueryExecutionContext={'Database': DB},
            ResultConfiguration={'OutputLocation': OUTPUT},
            WorkGroup=WG,
        )
        qid = resp['QueryExecutionId']
        wait_for_query(qid)

        # Fetch first page of results
        first_page = athena.get_query_results(QueryExecutionId=qid)
        rows = first_page['ResultSet']['Rows']
        if not rows:
            return {'results': [], 'row_count': 0}

        columns = [col.get('VarCharValue', '') for col in rows[0]['Data']]
        first_page_data = []
        for row in rows[1:]:
            values = [f.get('VarCharValue', '') for f in row['Data']]
            first_page_data.append(dict(zip(columns, values)))

        has_more_pages = 'NextToken' in first_page

        # Small result set with no more pages → return inline (fast path)
        if not has_more_pages and len(first_page_data) <= INLINE_ROW_LIMIT and not force_s3:
            return {
                'results': first_page_data,
                'row_count': len(first_page_data),
                'truncated': False,
                'query_execution_id': qid,
            }

        # Large result set OR force_s3 → point to Athena's own CSV output in S3.
        # Athena already wrote the full result as CSV at:
        #   <OUTPUT_LOCATION>/<query-execution-id>.csv
        # No need to paginate and re-write.
        s3_uri = get_athena_output_uri(qid)
        logger.info(f"Large result set — S3 URI: {s3_uri}")

        # We only have the first page count; if there are more pages the exact
        # total is unknown without full pagination, but that's fine — the agent
        # just needs the s3_uri and the preview.
        estimated_rows = f'>{len(first_page_data)}' if has_more_pages else len(first_page_data)

        return {
            'results': first_page_data[:PREVIEW_ROW_COUNT],
            'row_count': estimated_rows,
            'truncated': True,
            'preview_rows': min(PREVIEW_ROW_COUNT, len(first_page_data)),
            'columns': columns,
            's3_uri': s3_uri,
            'query_execution_id': qid,
            'message': (
                f'Query returned {estimated_rows} rows. '
                f'Full dataset available as CSV in S3. Use the Code Interpreter to '
                f'download it with: aws s3 cp {s3_uri} /tmp/data.csv'
            ),
        }

    except Exception as e:
        logger.error(f"Query error: {e}")
        return {'error': str(e)}


def get_athena_output_uri(qid):
    """Get the S3 URI of Athena's CSV output for a completed query.

    Athena writes results to: <OutputLocation>/<QueryExecutionId>.csv
    GetQueryExecution returns the OutputLocation which may be just the prefix
    or the full path depending on the workgroup config.
    """
    r = athena.get_query_execution(QueryExecutionId=qid)
    output_location = r['QueryExecution']['ResultConfiguration']['OutputLocation']

    # If it already ends with .csv, it's the full path
    if output_location.endswith('.csv'):
        return output_location

    # Otherwise it's the prefix — append the query ID
    prefix = output_location.rstrip('/')
    return f'{prefix}/{qid}.csv'


def wait_for_query(qid, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        r = athena.get_query_execution(QueryExecutionId=qid)
        state = r['QueryExecution']['Status']['State']
        if state == 'SUCCEEDED':
            return
        if state in ('FAILED', 'CANCELLED'):
            reason = r['QueryExecution']['Status'].get('StateChangeReason', '')
            raise RuntimeError(f"Query {state}: {reason}")
        time.sleep(1)
    raise TimeoutError(f"Query timed out after {timeout}s")
