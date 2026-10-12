#!/usr/bin/env python3
"""Mail a plain-text alarm when a scheduled digest run fails.

Why this exists: every interesting failure of the weekly digest -- a pipeline
crash, an expired SMTP app password, an exhausted API budget, a GitHub outage
mid-run -- produces exactly the same thing in the inbox, which is nothing. A
red run in a tab nobody opens is not a notification.

Deliberately minimal. It shares `load_config()` with the pipeline so the SMTP
settings cannot drift, but it renders no HTML, reads no digest and makes no
model calls: the one job it has is to work on the day everything else did not.
"""

from __future__ import annotations

import argparse
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.config import load_config  # noqa: E402

BODY = """\
The weekly quant-ph digest did not go out.

Cron that fired:  {cron}
Failed run:       {run_url}

What happens next, without you doing anything:

  * If this was a Monday rung, the remaining rungs that day will try again --
    they are scheduled a few hours apart precisely for this.
  * If every Monday rung fails, the Tuesday-morning catch-up will try once
    more and record the send if it succeeds.
  * If you are reading this on Tuesday, the catch-up is what failed and no
    digest will go out this week until someone looks.

Open the run above and read the "Run the pipeline" step. The usual causes, in
rough order of likelihood: the Gmail app password was revoked, the Anthropic
key hit its spending cap, or arXiv was unreachable for the whole window.

To send by hand once the cause is fixed:
  gh workflow run "Weekly Digest" -f mode=send
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-url", required=True, help="link to the failed Actions run")
    parser.add_argument("--cron", default="(unknown)", help="the cron expression that fired")
    args = parser.parse_args()

    cfg = load_config()
    email = cfg.email
    if not email.enabled:
        # Nothing to do, and nothing worth failing over: a repository with no
        # mail configured is allowed to have no alarm.
        print("email not configured (SMTP_* absent); no alarm sent", file=sys.stderr)
        return 0

    sender = email.sender or email.user or ""
    message = EmailMessage()
    message["Subject"] = f"{email.subject_prefix} FAILED - no digest this run".strip()
    message["From"] = f"{email.sender_name} <{sender}>" if email.sender_name else sender
    # The alarm goes to the same list as the digest. One recipient is addressed
    # directly; several go in Bcc, for the same reason the digest does it --
    # To:/Cc: would hand every subscriber's address to every other subscriber.
    if len(email.recipients) == 1:
        message["To"] = email.recipients[0]
    else:
        message["To"] = sender
        message["Bcc"] = ", ".join(email.recipients)
    # Not a digest, and must never be threaded with one or filtered like one.
    message["X-Scout-Alarm"] = "1"
    message.set_content(BODY.format(cron=args.cron, run_url=args.run_url))

    if email.port == 465:
        with smtplib.SMTP_SSL(email.host or "", email.port, timeout=60) as server:
            server.login(email.user or "", email.password or "")
            server.send_message(message)
    else:
        with smtplib.SMTP(email.host or "", email.port, timeout=60) as server:
            server.starttls()
            server.login(email.user or "", email.password or "")
            server.send_message(message)

    print(f"alarm sent to {len(email.recipients)} recipient(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
