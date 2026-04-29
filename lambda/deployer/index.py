"""Custom Resource Lambda for AgentCore Runtime deployment."""
import json
import logging
import time
import urllib3
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")
    request_type = event['RequestType']
    props = event['ResourceProperties']
    try:
        if request_type == 'Create':
            return handle_create(event, props)
        elif request_type == 'Update':
            return handle_update(event, props)
        elif request_type == 'Delete':
            return handle_delete(event, props)
        else:
            return send_response(event, 'FAILED', {'Error': f'Unknown RequestType: {request_type}'})
    except Exception as e:
        logger.error(f'Error: {e}', exc_info=True)
        return send_response(event, 'FAILED', {'Error': str(e)})


def handle_create(event, props):
    build_id = trigger_build(props)
    data = create_runtime(props)
    data['BuildId'] = build_id
    return send_response(event, 'SUCCESS', data)


def handle_update(event, props):
    build_id = trigger_build(props)
    region = props.get('Region', 'us-east-1')
    runtime_id = event.get('PhysicalResourceId', '')
    rid = runtime_id.split('/')[-1] if '/' in runtime_id else runtime_id
    client = boto3.client('bedrock-agentcore-control', region_name=region)
    env_vars = build_env_vars(props)
    try:
        client.update_agent_runtime(
            agentRuntimeId=rid,
            roleArn=props['ExecutionRoleArn'],
            agentRuntimeArtifact={'containerConfiguration': {
                'containerUri': f"{props['EcrRepositoryUri']}:production",
            }},
            networkConfiguration={'networkMode': 'PUBLIC'},
            environmentVariables=env_vars,
        )
        wait_active(client, rid)
        encoded_arn = runtime_id.replace('/', '%2F').replace(':', '%3A')
        return send_response(event, 'SUCCESS', {
            'RuntimeArn': runtime_id, 'RuntimeId': rid,
            'ApiEndpoint': f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations",
            'BuildId': build_id,
        })
    except Exception as e:
        logger.warning(f'Update failed, recreating: {e}')
        if runtime_id and runtime_id.startswith('arn:'):
            try:
                client.delete_agent_runtime(agentRuntimeId=rid)
                time.sleep(30)
            except Exception:
                pass
        data = create_runtime(props)
        data['BuildId'] = build_id
        return send_response(event, 'SUCCESS', data)


def handle_delete(event, props):
    region = props.get('Region', 'us-east-1')
    runtime_id = event.get('PhysicalResourceId', '')
    client = boto3.client('bedrock-agentcore-control', region_name=region)

    if runtime_id and runtime_id.startswith('arn:'):
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id.split('/')[-1])
        except Exception as e:
            logger.warning(f'Delete runtime error: {e}')

    return send_response(event, 'SUCCESS', {'Status': 'DELETED'})


def create_runtime(props):
    region = props.get('Region', 'us-east-1')
    name = props.get('AgentName', 'agent').replace('-', '_')
    client = boto3.client('bedrock-agentcore-control', region_name=region)
    image = f"{props['EcrRepositoryUri']}:production"
    env_vars = build_env_vars(props)

    resp = client.create_agent_runtime(
        agentRuntimeName=name,
        description=f'{name} AgentCore Runtime',
        roleArn=props['ExecutionRoleArn'],
        agentRuntimeArtifact={'containerConfiguration': {'containerUri': image}},
        networkConfiguration={'networkMode': 'PUBLIC'},
        environmentVariables=env_vars,
        protocolConfiguration={'serverProtocol': 'HTTP'},
    )
    rid = resp['agentRuntimeId']
    arn = resp['agentRuntimeArn']
    wait_active(client, rid)
    encoded_arn = arn.replace('/', '%2F').replace(':', '%3A')
    return {
        'RuntimeArn': arn, 'RuntimeId': rid,
        'ApiEndpoint': f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations",
    }


def build_env_vars(props):
    env = {
        'AGENT_NAME': props.get('AgentName', 'ses-analytics'),
        'AWS_REGION': props.get('Region', 'us-east-1'),
        'LOG_LEVEL': 'INFO',
    }
    for key, env_key in [
        ('GatewayUrl', 'GATEWAY_MCP_URL'),
        ('ModelId', 'MODEL_ID'),
        ('DatabaseName', 'DATABASE_NAME'),
        ('AthenaWorkGroup', 'ATHENA_WORKGROUP'),
        ('AthenaResultsBucket', 'ATHENA_RESULTS_BUCKET'),
        ('CodeInterpreterId', 'CODE_INTERPRETER_ID'),
        ('MemoryId', 'MEMORY_ID'),
        ('GuardrailId', 'GUARDRAIL_ID'),
        ('GuardrailVersion', 'GUARDRAIL_VERSION'),
    ]:
        val = props.get(key, '')
        if val:
            env[env_key] = val

    return env


def wait_active(client, rid, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = client.get_agent_runtime(agentRuntimeId=rid)
            # The response may nest under 'agentRuntime' or be flat
            runtime = resp.get('agentRuntime', resp)
            status = runtime.get('status', runtime.get('Status', ''))
            logger.info(f'Runtime status: {status}')
            if status in ('ACTIVE', 'READY'):
                return
            if status in ('FAILED', 'STOPPED'):
                reasons = runtime.get('failureReasons', [])
                raise RuntimeError(f'Runtime {status}: {reasons}')
        except Exception as e:
            if 'ResourceNotFoundException' not in str(e) and 'FAILED' not in str(e):
                logger.warning(f'Status check: {e}')
        time.sleep(10)
    logger.warning(f'Runtime timed out after {timeout}s — proceeding anyway')


def trigger_build(props):
    region = props.get('Region', 'us-east-1')
    cb = boto3.client('codebuild', region_name=region)
    project = props['BuildProject']
    resp = cb.start_build(projectName=project)
    build_id = resp['build']['id']

    start = time.time()
    while time.time() - start < 1200:
        r = cb.batch_get_builds(ids=[build_id])
        status = r['builds'][0]['buildStatus']
        logger.info(f'Build: {status}')
        if status == 'SUCCEEDED':
            return build_id
        if status in ('FAILED', 'FAULT', 'TIMED_OUT', 'STOPPED'):
            logs_info = r['builds'][0].get('logs', {})
            raise RuntimeError(f'Build {status}: {logs_info.get("groupName")}/{logs_info.get("streamName")}')
        time.sleep(30)
    raise TimeoutError('Build timed out')


def send_response(event, status, data):
    reason = data.get('Error', f'See CloudWatch logs for RequestId: {event["RequestId"]}')
    body = json.dumps({
        'Status': status,
        'Reason': reason,
        'PhysicalResourceId': data.get('RuntimeArn', event.get('PhysicalResourceId', 'runtime')),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data,
    })
    http = urllib3.PoolManager()
    http.request('PUT', event['ResponseURL'], body=body.encode('utf-8'),
                 headers={'Content-Type': 'application/json'})
    return {'Status': status, 'Data': data}
