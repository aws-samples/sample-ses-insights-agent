#!/usr/bin/env python3
"""
Interactive CLI to seed Amazon Simple Email Service (Amazon SES) analytics data via the Amazon SES Mailbox Simulator.

Sends real emails through SES to simulator addresses that generate deliveries,
bounces, complaints, and auto-responses. Events flow through the full pipeline:
SES → Configuration Set → Firehose → S3 Parquet → Glue → Athena.

Usage:
    python3 scripts/seed_sample_data.py                    # interactive mode
    python3 scripts/seed_sample_data.py --profile my-aws   # with named profile
    python3 scripts/seed_sample_data.py --region eu-west-1  # different region
"""
import argparse
import math
import sys
import time

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# SES Mailbox Simulator addresses
SIMULATOR_SCENARIOS = {
    'success':         ('success@simulator.amazonses.com', 'Successful delivery'),
    'bounce':          ('bounce@simulator.amazonses.com', 'Hard bounce (permanent)'),
    'complaint':       ('complaint@simulator.amazonses.com', 'Spam complaint'),
    'ooto':            ('ooto@simulator.amazonses.com', 'Out-of-office + delivery'),
    'suppressionlist': ('suppressionlist@simulator.amazonses.com', 'Suppression list bounce'),
}

SUBJECTS = [
    'Your order has shipped',
    'Weekly newsletter',
    'Account security alert',
    'Welcome to our service',
    'Your invoice is ready',
    'Password reset request',
    'New feature announcement',
    'Monthly report',
]


def prompt_choice(prompt: str, options: list, allow_custom: bool = False) -> str:
    """Display numbered options and return the selected value."""
    print(f"\n{prompt}")
    for i, (value, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    if allow_custom:
        print(f"  {len(options) + 1}) Enter a custom value")

    while True:
        try:
            raw = input("\n👉 Choice: ").strip()
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
            if allow_custom and idx == len(options) + 1:
                return input("   Enter value: ").strip()
        except (ValueError, EOFError, KeyboardInterrupt):
            pass
        print("   Invalid choice, try again.")


def prompt_int(prompt: str, default: int, min_val: int = 1, max_val: int = 10000) -> int:
    """Prompt for an integer with a default."""
    while True:
        try:
            raw = input(f"{prompt} [{default}]: ").strip()
            if not raw:
                return default
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            print(f"   Must be between {min_val} and {max_val}.")
        except (ValueError, EOFError, KeyboardInterrupt):
            print("   Enter a number.")


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s" if secs else f"{minutes}m"


def fetch_verified_identities(session, region: str) -> tuple:
    """Fetch verified email addresses and domains from SES."""
    ses = session.client('sesv2', region_name=region)
    emails = []
    domains = []
    try:
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs['NextToken'] = next_token
            resp = ses.list_email_identities(**kwargs)
            for identity in resp.get('EmailIdentities', []):
                name = identity['IdentityName']
                status = identity.get('SendingEnabled', False)
                if not status:
                    continue
                if identity['IdentityType'] == 'EMAIL_ADDRESS':
                    emails.append(name)
                elif identity['IdentityType'] == 'DOMAIN':
                    domains.append(name)
            next_token = resp.get('NextToken')
            if not next_token:
                break
    except Exception as e:
        print(f"  ⚠️  Could not list identities: {e}")
    return emails, domains


def get_send_rate(session, region: str) -> int:
    """Get the account's max SES send rate (emails/second)."""
    ses = session.client('sesv2', region_name=region)
    try:
        resp = ses.get_account()
        rate = resp.get('SendQuota', {}).get('MaxSendRate', 1.0)
        return max(1, int(rate))
    except Exception:
        return 1


def get_config_set_name(session, region: str) -> str:
    """Auto-discover the SES configuration set from CloudFormation."""
    cfn = session.client('cloudformation', region_name=region)
    paginator = cfn.get_paginator('list_stacks')
    for page in paginator.paginate(StackStatusFilter=[
        'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE',
    ]):
        for s in page['StackSummaries']:
            if 'ses-analytics' not in s['StackName'].lower():
                continue
            details = cfn.describe_stacks(StackName=s['StackName'])
            outputs = {
                o['OutputKey']: o['OutputValue']
                for o in details['Stacks'][0].get('Outputs', [])
            }
            cs = outputs.get('SesConfigurationSetName', '')
            if cs:
                return cs
    return ''


def choose_sender(emails: list, domains: list) -> str:
    """Interactive sender selection from verified identities."""
    options = []
    for email in emails:
        options.append((email, f"📧 {email}"))
    for domain in domains:
        options.append((f"@{domain}", f"🌐 {domain} (you'll type the local part)"))

    if not options:
        print("\n⚠️  No verified sending identities found in this account/region.")
        print("   You need at least one verified email or domain in SES.")
        addr = input("\n👉 Enter a verified sender email manually: ").strip()
        return addr

    choice = prompt_choice("Select a sender identity:", options)

    if choice.startswith('@'):
        domain = choice[1:]
        local = input(f"   Enter the local part (before @{domain}): ").strip()
        return f"{local}@{domain}"
    return choice


def choose_distribution(count: int) -> list:
    """Interactive scenario distribution selection."""
    print("\n📊 Choose email distribution:")
    print(f"   Total emails: {count}\n")

    scenarios = list(SIMULATOR_SCENARIOS.keys())
    plan = []
    remaining = count

    for i, scenario in enumerate(scenarios):
        addr, desc = SIMULATOR_SCENARIOS[scenario]
        is_last = (i == len(scenarios) - 1)

        if is_last:
            n = remaining
            print(f"   {scenario} ({desc}): {n} (remaining)")
        else:
            default = round(remaining * {
                'success': 0.70, 'bounce': 0.20,
                'complaint': 0.10, 'ooto': 0.05,
                'suppressionlist': 0.03,
            }.get(scenario, 0.1) * count / remaining) if remaining > 0 else 0
            default = min(default, remaining)
            n = prompt_int(
                f"   {scenario} ({desc})",
                default=default, min_val=0, max_val=remaining,
            )
        remaining -= n

        for j in range(n):
            local, domain = addr.split('@')
            recipient = f"{local}+{len(plan)}@{domain}"
            subject = SUBJECTS[len(plan) % len(SUBJECTS)]
            plan.append((scenario, recipient, subject))

    return plan


def send_emails(session, region, sender, config_set, plan, tps):
    """Send emails respecting the account TPS, with progress bar."""
    ses = session.client('sesv2', region_name=region)
    sent = 0
    errors = 0
    total = len(plan)
    bar_width = 40

    for scenario, recipient, subject in plan:
        try:
            ses.send_email(
                FromEmailAddress=sender,
                Destination={'ToAddresses': [recipient]},
                Content={'Simple': {
                    'Subject': {'Data': f'[Test] {subject}'},
                    'Body': {'Text': {'Data':
                        f'SES analytics seed email.\n'
                        f'Scenario: {scenario}\n'
                        f'Recipient: {recipient}'}},
                }},
                ConfigurationSetName=config_set,
            )
            sent += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                # Clear progress bar, print error, then redraw
                sys.stdout.write('\r' + ' ' * 70 + '\r')
                print(f"  ⚠️  Error ({scenario}): {e}")

        if sent % tps == 0:
            time.sleep(1.0)

        # Draw progress bar
        progress = (sent + errors) / total
        filled = int(bar_width * progress)
        bar = '█' * filled + '░' * (bar_width - filled)
        pct = int(progress * 100)
        sys.stdout.write(f'\r  [{bar}] {pct}% ({sent}/{total} sent, {errors} err)')
        sys.stdout.flush()

    sys.stdout.write('\n')
    return sent, errors


def _friendly_auth_message(e: Exception) -> str:
    """Return a user-friendly message for common AWS auth errors."""
    if isinstance(e, NoCredentialsError):
        return "No AWS credentials found. Run 'aws configure' or set AWS_PROFILE."
    error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
    if error_code in ('ExpiredTokenException', 'ExpiredToken', 'InvalidClientTokenId'):
        return "Your AWS session token has expired. Please refresh your credentials (e.g. 'aws sso login' or re-export temporary credentials)."
    if error_code == 'AccessDeniedException':
        return f"Access denied. Check that your IAM role/user has the required permissions.\n   {e}"
    return str(e)


def main():
    parser = argparse.ArgumentParser(description='Interactive SES analytics seeder')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--profile', help='AWS named profile')
    args = parser.parse_args()

    print("=" * 55)
    print("  SES Analytics — Seed Data via Mailbox Simulator")
    print("=" * 55)

    session = (boto3.Session(profile_name=args.profile)
               if args.profile else boto3.Session())
    region = args.region

    # 1. Discover config set
    print("\n🔍 Discovering SES Configuration Set...")
    try:
        config_set = get_config_set_name(session, region)
    except (ClientError, NoCredentialsError) as e:
        print(f"❌ AWS credentials error: {_friendly_auth_message(e)}")
        sys.exit(1)
    if not config_set:
        print("❌ No ses-analytics stack found.")
        sys.exit(1)
    print(f"   ✅ {config_set}")

    # 2. Get TPS
    try:
        tps = get_send_rate(session, region)
    except (ClientError, NoCredentialsError) as e:
        print(f"❌ AWS credentials error: {_friendly_auth_message(e)}")
        sys.exit(1)
    print(f"⚡ Account send rate: {tps} emails/sec")

    # 3. Fetch verified identities
    print("\n🔍 Fetching verified sending identities...")
    try:
        emails, domains = fetch_verified_identities(session, region)
    except (ClientError, NoCredentialsError) as e:
        print(f"❌ AWS credentials error: {_friendly_auth_message(e)}")
        sys.exit(1)
    print(f"   Found {len(emails)} email(s), {len(domains)} domain(s)")

    # 4. Choose sender
    sender = choose_sender(emails, domains)
    print(f"\n📧 Sender: {sender}")

    # 5. Choose count
    count = prompt_int("\n📬 How many emails to send?", default=50,
                       min_val=1, max_val=10000)

    # 6. Choose distribution
    plan = choose_distribution(count)

    # 7. Show summary and estimate
    estimated = math.ceil(len(plan) / tps)
    breakdown = {}
    for s, _, _ in plan:
        breakdown[s] = breakdown.get(s, 0) + 1

    print(f"\n{'─' * 45}")
    print(f"  Summary")
    print(f"{'─' * 45}")
    print(f"  Sender:    {sender}")
    print(f"  Config:    {config_set}")
    print(f"  Total:     {len(plan)} emails")
    print(f"  Estimate:  ~{format_duration(estimated)}")
    for s, n in sorted(breakdown.items()):
        _, desc = SIMULATOR_SCENARIOS[s]
        print(f"    {s:20s} {n:>4d}  ({desc})")
    print(f"{'─' * 45}")

    # 8. Confirm
    confirm = input("\n🚀 Send? (y/n): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("Cancelled.")
        return

    # 9. Send
    print()
    sent, errs = send_emails(session, region, sender, config_set,
                             plan, tps)

    print(f"\n🎉 Done! Sent {sent} emails ({errs} errors).")
    print(f"   Events appear in Athena after Firehose flush (~1-5 min).")
    print(f"   Then: python3 test-client/chat.py -q 'show delivery stats'")


if __name__ == '__main__':
    main()
