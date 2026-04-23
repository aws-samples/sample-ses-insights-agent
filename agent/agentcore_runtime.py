"""
Amazon Simple Email Service (Amazon SES) Analytics AgentCore Runtime — Strands SDK with Gateway MCP tools
and Code Interpreter for EDA.

Users connect via MCP and query SES analytics using natural language.
The agent translates to Athena SQL via Gateway tools and can perform
exploratory data analysis using the Code Interpreter.

Conversation history is managed by AgentCore Memory (short-term) so
follow-up questions work naturally within a session.
"""
import os
import re
import logging
from datetime import datetime

from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands_tools.code_interpreter import AgentCoreCodeInterpreter
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

REGION = os.environ.get('AWS_REGION', 'us-east-1')
AGENT_NAME = os.environ.get('AGENT_NAME', 'ses-analytics')
GATEWAY_MCP_URL = os.environ.get('GATEWAY_MCP_URL', '')
MODEL_ID = os.environ.get('MODEL_ID', 'us.anthropic.claude-sonnet-4-20250514-v1:0')
DATABASE_NAME = os.environ.get('DATABASE_NAME', '')
ATHENA_WORKGROUP = os.environ.get('ATHENA_WORKGROUP', '')
CODE_INTERPRETER_ID = os.environ.get('CODE_INTERPRETER_ID', '')
MEMORY_ID = os.environ.get('MEMORY_ID', '')
GUARDRAIL_ID = os.environ.get('GUARDRAIL_ID', '')
GUARDRAIL_VERSION = os.environ.get('GUARDRAIL_VERSION', '')


def build_system_prompt(gateway_connected: bool) -> str:
    """Build the system prompt at invocation time when env vars are available."""
    db = os.environ.get('DATABASE_NAME', 'ses_analytics_dev')

    # If the gateway tools aren't available, prepend guidance so the agent
    # can tell the user what's wrong instead of silently having no tools.
    if not gateway_connected:
        missing_tools_notice = """
## ⚠️ Gateway tools are not available
The MCP Gateway connection failed or was not configured. This means the
query_ses_analytics, describe_ses_schema, and get_delivery_summary tools
are unavailable. You can only use the Code Interpreter and get_current_time.

If a user asks to query data, let them know:
- The analytics pipeline or MCP Gateway may not be deployed yet.
- They should verify that `enableGateway: true` is set in cdk.json and run `cdk deploy`.
- Check the GATEWAY_MCP_URL environment variable is set on this runtime.
- Check CloudWatch logs for connection errors.

"""
    else:
        missing_tools_notice = ""

    return f"""{missing_tools_notice}You are an SES Analytics Assistant with access to Amazon SES email event data
stored in an Athena data lake. You help users understand their email sending
performance through natural language queries.

## Your capabilities
1. **Query SES analytics** — translate natural language questions into Athena SQL
   and return results. Use the `query_ses_analytics` tool.
2. **Schema discovery** — describe available tables and columns using
   `describe_ses_schema`.
3. **Quick summaries** — get delivery metrics snapshots using `get_delivery_summary`.
4. **Exploratory data analysis** — use the Code Interpreter to run Python/pandas
   analysis on query results, generate charts, and compute statistics.

## Database info
- Database: `{db}`
- Main table: `ses_events` (partitioned by year, month, day, hour)
- Partition columns are STRING type — always use string comparisons:
  WHERE year = '2026' AND month = '04' AND day = '21'
  Do NOT use integers: WHERE year = 2026 (this will fail)
- Event types: Send, Delivery, Bounce, Complaint, Open, Click, Reject, Rendering Failure
- Key fields: eventtype, mail.timestamp, mail.source, mail.messageid,
  mail.destination, mail.commonheaders.subject, bounce.bouncetype,
  complaint.complaintfeedbacktype, open.ipaddress, click.link
- Views: daily_summary, bounce_analysis, sender_reputation, hourly_volume

## Query guidelines
- Always use partition filters (year, month, day) for cost efficiency
- CRITICAL: Partition columns (year, month, day, hour) are VARCHAR/STRING type.
  Always quote values: year = '2026', month = '04', day = '21', hour = '14'
  Use zero-padded strings for month, day, hour: '04' not '4', '09' not '9'
- Use DISTINCT on mail.messageid to avoid counting duplicates
- For rates, calculate: deliveries/sends, opens/deliveries, bounces/sends
- Limit results to reasonable sizes (LIMIT 100 by default)
- When users ask vague questions, start with get_delivery_summary for context
- Use the pre-built views (daily_summary, bounce_analysis, sender_reputation)
  for common queries — they're faster and pre-aggregated

## EDA workflow
When users want deeper analysis:
1. First query the data using query_ses_analytics
2. If the response has `truncated: true` and an `s3_uri`, use the Code Interpreter
   to download the CSV directly from S3 using `aws s3 cp`, then load it with pandas.
   The Code Interpreter has S3 read access to the analytics bucket.
3. If the response is NOT truncated, pass the inline `results` directly to the
   Code Interpreter as a list of dicts.
4. Generate visualizations (matplotlib charts)
5. Provide insights and recommendations

## Large dataset workflow
When query_ses_analytics returns more than 500 rows:
- The response will contain `truncated: true`, an `s3_uri`, and a small `results`
  preview (first 10 rows) so you can see the shape of the data.
- In a SINGLE code_interpreter call, use executeCommand to download the file from S3
  and then load it with pandas. Example pattern:

```python
import subprocess
subprocess.run(['aws', 's3', 'cp', '<s3_uri>', '/tmp/data.csv'], check=True)

import pandas as pd
df = pd.read_csv('/tmp/data.csv')
print(f"Loaded {{len(df)}} rows, {{len(df.columns)}} columns")
print(df.head())
```

- Then proceed with analysis and charting as normal.
- Do NOT try to re-query with a smaller LIMIT just because the result was large.
  The user asked for that data — download it from S3 and analyze it.

## Testing the S3 path
The query_ses_analytics tool accepts a `force_s3: true` parameter that forces
results to go through S3 even for small datasets. If a user asks to "force S3"
or "test the S3 path", pass `force_s3: true` in the tool call.

## Code Interpreter chart output
When generating charts, you MUST put ALL code in a SINGLE code_interpreter call.
Do NOT split chart generation and base64 encoding into separate calls — variables
won't persist between calls. Use this exact pattern:

```python
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import base64, io

# --- your data and plotting code here ---
data = <query_results>
df = pd.DataFrame(data)
# ... transform and plot ...

fig, ax = plt.subplots(figsize=(10, 5))
# ... ax.plot / ax.bar / etc ...
plt.tight_layout()

# --- encode and print (MUST be in the same call) ---
buf = io.BytesIO()
fig.savefig(buf, format='png', dpi=150)
buf.seek(0)
print('__IMG_BASE64_START__' + base64.b64encode(buf.read()).decode() + '__IMG_BASE64_END__')
plt.close()
```

CRITICAL RULES for charts:
- ALL code MUST be in ONE single code_interpreter call (data prep + plot + base64 encode)
- Use matplotlib.use('Agg') FIRST, before importing pyplot
- Do NOT use plt.show() — it will fail in the sandbox
- Do NOT use seaborn — it is not installed
- ALWAYS end with the __IMG_BASE64_START__...__IMG_BASE64_END__ print pattern
- The client will detect the markers and save the PNG locally

## EDA workflow example
When a user asks "plot my bounce rate trend over the last 30 days":

1. Query the data with query_ses_analytics
2. If truncated: in ONE code_interpreter call:
   - Download CSV from S3 with subprocess + aws s3 cp
   - Load with pd.read_csv('/tmp/data.csv')
   - Transform, plot, encode as base64 with markers
3. If NOT truncated: put ALL of this in ONE code_interpreter call:
   - Create the DataFrame from the inline query results
   - Transform the data (parse dates, compute rates)
   - Plot with matplotlib
   - Encode as base64 and print with markers

This pattern works for any metric: delivery rates, complaint trends, hourly
volume heatmaps, sender comparisons, etc. Always query first, then visualize.

Be concise, accurate, and proactive about suggesting useful analyses."""


@tool
def get_current_time() -> str:
    """Get the current UTC date and time."""
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def now():
    return datetime.now().isoformat()


def normalize_event(event):
    """Normalize Strands streaming events into SSE events."""
    out = []
    if hasattr(event, '__dict__'):
        ev = event.__dict__
    elif isinstance(event, dict):
        ev = event
    else:
        return out

    inner = ev.get('event') or ev

    cbd = inner.get('contentBlockDelta')
    if cbd:
        text = cbd.get('delta', {}).get('text')
        if text:
            out.append({'type': 'assistant_delta', 'ts': now(), 'data': {'text': text}})
        return out

    cbs = inner.get('contentBlockStart', {})
    tu = cbs.get('start', {}).get('toolUse')
    if tu and tu.get('name'):
        out.append({'type': 'tool_start', 'ts': now(), 'data': {'name': tu['name'], 'toolUseId': tu.get('toolUseId', '')}})
        return out

    if inner.get('contentBlockStop') is not None:
        out.append({'type': 'tool_end', 'ts': now(), 'data': {}})

    # Check for image markers in tool results and emit as a dedicated event
    _extract_image_markers(inner, out)

    return out


# Track already-emitted image hashes to avoid duplicates within a single invocation
_emitted_images: set = set()


def _reset_image_tracking():
    """Reset image deduplication state for a new invocation."""
    _emitted_images.clear()


def _extract_image_markers(obj, out):
    """Recursively search an event for __IMG_BASE64_START__ markers and emit them once."""
    if isinstance(obj, str):
        if '__IMG_BASE64_START__' in obj and '__IMG_BASE64_END__' in obj:
            for match in re.finditer(r'__IMG_BASE64_START__(.+?)__IMG_BASE64_END__', obj, re.DOTALL):
                b64 = match.group(1).strip()
                # Deduplicate by first 64 chars of the base64 data
                sig = b64[:64]
                if sig not in _emitted_images:
                    _emitted_images.add(sig)
                    out.append({
                        'type': 'image',
                        'ts': now(),
                        'data': {'base64': b64},
                    })
    elif isinstance(obj, dict):
        for v in obj.values():
            _extract_image_markers(v, out)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _extract_image_markers(item, out)


@app.entrypoint
async def agent_invocation(payload, context):
    """Main agent entrypoint with AgentCore Memory for conversation history."""
    prompt = payload.get('prompt', payload.get('message', ''))
    session_id = payload.get('sessionId', f'session-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}')

    # Reset per-invocation state
    _reset_image_tracking()

    # Build tools
    ci_kwargs = {'region': REGION}
    if CODE_INTERPRETER_ID:
        ci_kwargs['identifier'] = CODE_INTERPRETER_ID
        logger.info(f"Using custom Code Interpreter: {CODE_INTERPRETER_ID}")
    code_interpreter = AgentCoreCodeInterpreter(**ci_kwargs)

    tools = [get_current_time, code_interpreter.code_interpreter]

    # Connect Gateway MCP tools
    mcp_client = None
    gateway_connected = False
    if GATEWAY_MCP_URL:
        try:
            mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
                endpoint=GATEWAY_MCP_URL,
                aws_region=REGION,
                aws_service='bedrock-agentcore',
            ))
            mcp_client.__enter__()
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)
            gateway_connected = True
            logger.info(f"Gateway connected: {len(gateway_tools)} tools")
        except Exception as e:
            logger.warning(f"Gateway connection failed: {e}")
            mcp_client = None
    else:
        logger.warning("GATEWAY_MCP_URL not set - gateway tools unavailable")

    # Set up AgentCore Memory session manager for conversation history
    session_manager = None
    if MEMORY_ID:
        try:
            memory_config = AgentCoreMemoryConfig(
                memory_id=MEMORY_ID,
                session_id=session_id,
                actor_id=session_id,
            )
            session_manager = AgentCoreMemorySessionManager(
                agentcore_memory_config=memory_config,
                region_name=REGION,
            )
            logger.info(f"Memory enabled: memory={MEMORY_ID}, session={session_id}")
        except Exception as e:
            logger.warning(f"Failed to initialize AgentCore Memory: {e}")
            session_manager = None
    else:
        logger.info("MEMORY_ID not set - conversation history disabled")

    model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=True,
                         **({"guardrail_id": GUARDRAIL_ID,
                             "guardrail_version": GUARDRAIL_VERSION} if GUARDRAIL_ID else {})
                         )
    agent = Agent(
        model=model,
        system_prompt=build_system_prompt(gateway_connected),
        tools=tools,
        session_manager=session_manager,
    )

    try:
        async for event in agent.stream_async(prompt):
            for normalized in normalize_event(event):
                yield normalized
        yield {'type': 'complete', 'ts': now(), 'data': {'status': 'done'}}
    finally:
        if mcp_client:
            mcp_client.__exit__(None, None, None)


if __name__ == '__main__':
    app.run()
