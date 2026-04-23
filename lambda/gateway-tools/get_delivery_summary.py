"""Gateway tool: Quick Amazon Simple Email Service (Amazon SES) delivery summary metrics."""
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

# Simple email validation pattern
EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')


def lambda_handler(event, context):
    """Return a summary of SES delivery metrics for the given period."""
    logger.info(f"Event: {json.dumps(event)}")

    days = min(max(int(event.get('days', 7)), 1), 90)
    sender = event.get('sender', '').strip()
    recipient = event.get('recipient', '').strip()
    event_type = event.get('event_type', '').strip()
    group_by = event.get('group_by', '').strip().lower()

    # Validate emails if provided
    if sender and not EMAIL_RE.match(sender):
        return {'error': f'Invalid sender email format: {sender}'}
    if recipient and not EMAIL_RE.match(recipient):
        return {'error': f'Invalid recipient email format: {recipient}'}

    # Validate event_type
    valid_event_types = {'send', 'delivery', 'bounce', 'complaint', 'open', 'click', 'reject'}
    if event_type and event_type.lower() not in valid_event_types:
        return {'error': f'Invalid event_type: {event_type}. Valid: {", ".join(sorted(valid_event_types))}'}

    # Validate group_by
    valid_group_by = {'', 'day', 'sender', 'event_type'}
    if group_by and group_by not in valid_group_by:
        return {'error': f'Invalid group_by: {group_by}. Valid: {", ".join(sorted(valid_group_by - {""}))}'}

    where_clause = f"WHERE mail.timestamp >= date_format(current_timestamp - interval '{days}' day, '%Y-%m-%dT%H:%i:%s.000Z')"
    params = []
    if sender:
        where_clause += " AND mail.source = ?"
        params.append(sender)
    if recipient:
        where_clause += " AND element_at(mail.destination, 1) = ?"
        params.append(recipient)
    if event_type:
        where_clause += " AND eventtype = ?"
        params.append(event_type.capitalize())

    # Build query based on group_by
    if group_by == 'day':
        sql = f"""
    SELECT
      DATE(from_iso8601_timestamp(mail.timestamp)) as date,
      COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END) as sends,
      COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) as deliveries,
      COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) as bounces,
      COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) as complaints,
      COUNT(DISTINCT CASE WHEN eventtype = 'Open' THEN mail.messageid END) as opens,
      COUNT(DISTINCT CASE WHEN eventtype = 'Click' THEN mail.messageid END) as clicks
    FROM {DB}.ses_events
    {where_clause}
    GROUP BY DATE(from_iso8601_timestamp(mail.timestamp))
    ORDER BY date DESC
    """
    elif group_by == 'sender':
        sql = f"""
    SELECT
      mail.source as sender,
      COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END) as sends,
      COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) as deliveries,
      COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) as bounces,
      COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) as complaints
    FROM {DB}.ses_events
    {where_clause}
    GROUP BY mail.source
    ORDER BY sends DESC
    LIMIT 50
    """
    elif group_by == 'event_type':
        sql = f"""
    SELECT
      eventtype,
      COUNT(DISTINCT mail.messageid) as message_count,
      COUNT(*) as event_count,
      MIN(mail.timestamp) as earliest,
      MAX(mail.timestamp) as latest
    FROM {DB}.ses_events
    {where_clause}
    GROUP BY eventtype
    ORDER BY event_count DESC
    """
    else:
        sql = f"""
    SELECT
      COUNT(DISTINCT CASE WHEN eventtype = 'Send' THEN mail.messageid END) as sends,
      COUNT(DISTINCT CASE WHEN eventtype = 'Delivery' THEN mail.messageid END) as deliveries,
      COUNT(DISTINCT CASE WHEN eventtype = 'Bounce' THEN mail.messageid END) as bounces,
      COUNT(DISTINCT CASE WHEN eventtype = 'Complaint' THEN mail.messageid END) as complaints,
      COUNT(DISTINCT CASE WHEN eventtype = 'Open' THEN mail.messageid END) as opens,
      COUNT(DISTINCT CASE WHEN eventtype = 'Click' THEN mail.messageid END) as clicks,
      COUNT(DISTINCT CASE WHEN eventtype = 'Reject' THEN mail.messageid END) as rejects,
      COUNT(DISTINCT mail.source) as unique_senders,
      COUNT(DISTINCT element_at(mail.destination, 1)) as unique_recipients,
      MIN(mail.timestamp) as earliest_event,
      MAX(mail.timestamp) as latest_event
    FROM {DB}.ses_events
    {where_clause}
    """

    try:
        exec_params = {
            'QueryString': sql,
            'QueryExecutionContext': {'Database': DB},
            'ResultConfiguration': {'OutputLocation': OUTPUT},
            'WorkGroup': WG,
        }
        if params:
            exec_params['ExecutionParameters'] = params

        resp = athena.start_query_execution(**exec_params)
        qid = resp['QueryExecutionId']
        wait_for_query(qid)

        results = athena.get_query_results(QueryExecutionId=qid)
        rows = results['ResultSet']['Rows']
        if len(rows) < 2:
            return {'summary': 'No data found for the specified period', 'days': days}

        columns = [c.get('VarCharValue', '') for c in rows[0]['Data']]

        # Grouped results return multiple rows
        if group_by:
            data = []
            for row in rows[1:]:
                values = [f.get('VarCharValue', '') for f in row['Data']]
                data.append(dict(zip(columns, values)))
            return {
                'period_days': days,
                'sender_filter': sender or 'all',
                'recipient_filter': recipient or 'all',
                'event_type_filter': event_type or 'all',
                'group_by': group_by,
                'results': data,
                'row_count': len(data),
            }

        # Flat summary (no group_by)
        values = [f.get('VarCharValue', '0') for f in rows[1]['Data']]
        metrics = dict(zip(columns, values))

        sends = int(metrics.get('sends', 0))
        deliveries = int(metrics.get('deliveries', 0))
        bounces = int(metrics.get('bounces', 0))
        complaints = int(metrics.get('complaints', 0))
        opens = int(metrics.get('opens', 0))
        clicks = int(metrics.get('clicks', 0))

        return {
            'period_days': days,
            'sender_filter': sender or 'all',
            'recipient_filter': recipient or 'all',
            'event_type_filter': event_type or 'all',
            'sends': sends,
            'deliveries': deliveries,
            'bounces': bounces,
            'complaints': complaints,
            'opens': opens,
            'clicks': clicks,
            'rejects': int(metrics.get('rejects', 0)),
            'unique_senders': int(metrics.get('unique_senders', 0)),
            'unique_recipients': int(metrics.get('unique_recipients', 0)),
            'delivery_rate': f"{(deliveries / sends * 100):.1f}%" if sends else 'N/A',
            'bounce_rate': f"{(bounces / sends * 100):.1f}%" if sends else 'N/A',
            'complaint_rate': f"{(complaints / deliveries * 100):.2f}%" if deliveries else 'N/A',
            'open_rate': f"{(opens / deliveries * 100):.1f}%" if deliveries else 'N/A',
            'click_rate': f"{(clicks / deliveries * 100):.1f}%" if deliveries else 'N/A',
            'earliest_event': metrics.get('earliest_event', ''),
            'latest_event': metrics.get('latest_event', ''),
        }

    except Exception as e:
        logger.error(f"Summary error: {e}")
        return {'error': str(e)}


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
