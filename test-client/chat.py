#!/usr/bin/env python3
"""
Amazon Simple Email Service (Amazon SES) Analytics MCP Test Client

A simple terminal chat client that connects to the AgentCore Runtime
using AWS Identity and Access Management (AWS IAM) credentials (SigV4).
No API keys needed — just your AWS credentials from the same account
where the stack is deployed.

Usage:
    python chat.py                                    # interactive mode
    python chat.py --runtime-id <ID>                  # specify runtime ID
    python chat.py --region us-east-1                 # specify region
    python chat.py --profile my-profile               # use named AWS profile
    python chat.py -q "show me bounces last 7 days"   # single query

The runtime ID is auto-discovered from CloudFormation stack outputs
if not provided.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def _ensure_venv():
    """Auto-create a venv and re-launch inside it if not already in one."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(script_dir, '.venv')
    venv_python = os.path.join(venv_dir, 'bin', 'python')
    req_file = os.path.join(script_dir, 'requirements.txt')

    # Already inside the venv — just install missing deps
    if sys.prefix != sys.base_prefix:
        if os.path.exists(req_file):
            missing = []
            with open(req_file) as f:
                for line in f:
                    pkg = line.strip()
                    if pkg and not pkg.startswith('#'):
                        try:
                            __import__(pkg)
                        except ImportError:
                            missing.append(pkg)
            if missing:
                print(f"📦 Installing missing dependencies: {', '.join(missing)}")
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', req_file, '-q'])
        return

    # Create venv if it doesn't exist
    if not os.path.exists(venv_python):
        print("🐍 Creating virtual environment (.venv)...")
        subprocess.check_call([sys.executable, '-m', 'venv', venv_dir])
        print("📦 Installing dependencies...")
        subprocess.check_call([venv_python, '-m', 'pip', 'install', '-r', req_file, '-q'])

    # Re-launch this script inside the venv
    os.execv(venv_python, [venv_python] + sys.argv)


_ensure_venv()

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, NoCredentialsError

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def make_session(profile: str = None) -> boto3.Session:
    """Create a boto3 session with optional named profile."""
    if profile:
        return boto3.Session(profile_name=profile)
    return boto3.Session()


def get_runtime_endpoint(session: boto3.Session, region: str, stack_name: str = None) -> tuple:
    """Auto-discover the AgentCore Runtime endpoint from CFN outputs."""
    cfn = session.client('cloudformation', region_name=region)

    paginator = cfn.get_paginator('list_stacks')
    for page in paginator.paginate(StackStatusFilter=['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']):
        for s in page['StackSummaries']:
            name = s['StackName']
            if stack_name and name != stack_name:
                continue
            if not stack_name and 'ses-analytics' not in name.lower():
                continue

            details = cfn.describe_stacks(StackName=name)
            outputs = {o['OutputKey']: o['OutputValue'] for o in details['Stacks'][0].get('Outputs', [])}

            endpoint = outputs.get('RuntimeApiEndpoint', outputs.get('ApiEndpoint', ''))
            runtime_arn = outputs.get('RuntimeRuntimeArn', outputs.get('RuntimeArn', ''))

            # CDK appends hash suffixes to output keys — fall back to partial matching
            if not endpoint:
                endpoint = next((v for k, v in outputs.items() if 'ApiEndpoint' in k), '')
            if not runtime_arn:
                runtime_arn = next((v for k, v in outputs.items() if 'RuntimeArn' in k), '')

            if endpoint or runtime_arn:
                rid = runtime_arn.split('/')[-1] if runtime_arn else ''
                if not endpoint and runtime_arn:
                    encoded_arn = runtime_arn.replace('/', '%2F').replace(':', '%3A')
                    endpoint = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations"
                return endpoint, rid

    return '', ''


def invoke_runtime(session: boto3.Session, endpoint: str, prompt: str, session_id: str, region: str) -> tuple:
    """Invoke the AgentCore Runtime with SigV4 auth. Returns (text, images) tuple."""
    credentials = session.get_credentials().get_frozen_credentials()

    url = endpoint if endpoint.endswith('/invocations') else f"{endpoint}/invocations"

    if not url.startswith('https://'):
        return "Error: endpoint must use HTTPS", []

    payload = json.dumps({
        'prompt': prompt,
        'sessionId': session_id,
    }).encode('utf-8')

    # Sign the request with SigV4
    aws_request = AWSRequest(method='POST', url=url, data=payload, headers={
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    SigV4Auth(credentials, 'bedrock-agentcore', region).add_auth(aws_request)

    # Use stdlib urllib with the exact signed headers and body
    req = Request(url, data=payload, method='POST')
    for key, value in aws_request.headers.items():
        req.add_header(key, value)

    try:
        with urlopen(req, timeout=300) as resp:
            body = b''
            while True:
                try:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    body += chunk
                except Exception:
                    break
            body = body.decode('utf-8')
    except HTTPError as e:
        return f"Error {e.code}: {e.read().decode('utf-8')}", []
    except Exception as e:
        return f"Connection error: {e}", []

    # Parse response — collect text and images separately
    full_text = ''
    images = []
    try:
        data = json.loads(body)
        if isinstance(data, list):
            for event in data:
                if event.get('type') == 'assistant_delta':
                    full_text += event.get('data', {}).get('text', '')
                elif event.get('type') == 'tool_start':
                    tool_name = event.get('data', {}).get('name', '')
                    full_text += f"\n🔧 Using tool: {tool_name}\n"
                elif event.get('type') == 'image':
                    b64 = event.get('data', {}).get('base64', '')
                    if b64:
                        images.append(b64)
        elif isinstance(data, dict):
            full_text = data.get('output', data.get('response', json.dumps(data, indent=2)))
    except json.JSONDecodeError:
        # Try line-by-line SSE parsing
        for line in body.split('\n'):
            line = line.strip()
            if not line or not line.startswith('data:'):
                continue
            try:
                event = json.loads(line[5:].strip())
                if event.get('type') == 'assistant_delta':
                    full_text += event.get('data', {}).get('text', '')
                elif event.get('type') == 'image':
                    b64 = event.get('data', {}).get('base64', '')
                    if b64:
                        images.append(b64)
            except Exception:
                pass

    # Also check text for any inline markers (belt and suspenders)
    if '__IMG_BASE64_START__' in full_text:
        for match in IMG_PATTERN.finditer(full_text):
            images.append(match.group(1).strip())
        full_text = IMG_PATTERN.sub('', full_text)

    return full_text or body, images


IMG_PATTERN = re.compile(r'__IMG_BASE64_START__(.+?)__IMG_BASE64_END__', re.DOTALL)
IMG_DIR = 'charts'


def save_images(images: list) -> list:
    """Decode and save base64 images to disk. Returns list of saved file paths."""
    if not images:
        return []

    os.makedirs(IMG_DIR, exist_ok=True)
    saved = []
    seen_hashes = set()
    for b64_data in images:
        # Pad if needed
        b64_clean = b64_data.strip()
        missing_padding = len(b64_clean) % 4
        if missing_padding:
            b64_clean += '=' * (4 - missing_padding)

        try:
            img_bytes = base64.b64decode(b64_clean)
        except Exception:
            continue

        # Only save valid PNG files (magic bytes: \x89PNG\r\n\x1a\n)
        if not img_bytes[:4] == b'\x89PNG':
            continue

        # Deduplicate by content hash
        content_hash = hashlib.md5(img_bytes, usedforsecurity=False).hexdigest()
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        filename = f"chart_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(IMG_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(img_bytes)
        saved.append(os.path.abspath(filepath))

    return saved


def print_response(text: str):
    if HAS_RICH:
        console.print(Panel(Markdown(text), title="SES Analytics Agent", border_style="cyan"))
    else:
        print(f"\n{'='*60}")
        print(text)
        print(f"{'='*60}\n")


def _friendly_auth_message(e: Exception) -> str:
    """Return a user-friendly message for common AWS auth errors."""
    msg = str(e)
    if isinstance(e, NoCredentialsError):
        return "No AWS credentials found. Run 'aws configure' or set AWS_PROFILE."
    error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
    if error_code in ('ExpiredTokenException', 'ExpiredToken', 'InvalidClientTokenId'):
        return "Your AWS session token has expired. Please refresh your credentials (e.g. 'aws sso login' or re-export temporary credentials)."
    if error_code == 'AccessDeniedException':
        return f"Access denied. Check that your IAM role/user has the required permissions.\n   {msg}"
    return msg


def main():
    parser = argparse.ArgumentParser(description='SES Analytics MCP Test Client')
    parser.add_argument('--runtime-id', help='AgentCore Runtime ID')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--profile', help='AWS named profile (from ~/.aws/config)')
    parser.add_argument('--stack-name', help='CloudFormation stack name')
    parser.add_argument('--query', '-q', help='Single query (non-interactive)')
    args = parser.parse_args()

    region = args.region
    session = make_session(args.profile)

    endpoint = ''
    runtime_id = args.runtime_id or ''

    if runtime_id:
        # runtime_id could be an ARN or just the ID
        if runtime_id.startswith('arn:'):
            encoded_arn = runtime_id.replace('/', '%2F').replace(':', '%3A')
            endpoint = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations"
        else:
            # Assume it's a short ID, construct the ARN
            try:
                account = session.client('sts').get_caller_identity()['Account']
            except (ClientError, NoCredentialsError) as e:
                print(f"❌ AWS credentials error: {_friendly_auth_message(e)}")
                sys.exit(1)
            arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
            encoded_arn = arn.replace('/', '%2F').replace(':', '%3A')
            endpoint = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations"
    else:
        print("🔍 Discovering AgentCore Runtime from CloudFormation...")
        try:
            endpoint, runtime_id = get_runtime_endpoint(session, region, args.stack_name)
        except (ClientError, NoCredentialsError) as e:
            print(f"❌ AWS credentials error: {_friendly_auth_message(e)}")
            sys.exit(1)
        if not endpoint:
            print("❌ Could not find SES Analytics stack. Use --runtime-id to specify manually.")
            sys.exit(1)

    print(f"✅ Runtime: {runtime_id}")
    print(f"📡 Endpoint: {endpoint}")
    if args.profile:
        print(f"🔑 Profile: {args.profile}")

    session_id = f"test-session-{uuid.uuid4().hex[:12]}-{'0' * 10}"

    if args.query:
        response, images = invoke_runtime(session, endpoint, args.query, session_id, region)
        saved = save_images(images)
        print_response(response)
        for path in saved:
            print(f"📊 Chart saved: {path}")
            print(f"   Open with: open {path}")
        return

    # Interactive mode
    print("\n💬 SES Analytics Chat (type 'quit' to exit)")
    print("   Try: 'show me delivery stats for the last 7 days'")
    print("   Try: 'what are my top bounce reasons?'")
    print("   Try: 'describe the database schema'")
    print("   Try: 'plot my daily bounce rate trend as a line chart'\n")

    while True:
        try:
            if HAS_RICH:
                prompt = console.input("[bold green]You:[/] ")
            else:
                prompt = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Bye!")
            break

        if prompt.strip().lower() in ('quit', 'exit', 'q'):
            print("👋 Bye!")
            break

        if not prompt.strip():
            continue

        print("⏳ Thinking...")
        response, images = invoke_runtime(session, endpoint, prompt, session_id, region)
        saved = save_images(images)
        print_response(response)
        for path in saved:
            print(f"📊 Chart saved: {path}")
            print(f"   Open with: open {path}")


if __name__ == '__main__':
    main()
