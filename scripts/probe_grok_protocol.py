#!/usr/bin/env python3
"""Smoke-test Grok protocol edge (no account creation).

Usage:
  uv run python scripts/probe_grok_protocol.py
  uv run python scripts/probe_grok_protocol.py --proxy http://127.0.0.1:7890
  uv run python scripts/probe_grok_protocol.py --clearance never

Exits 0 when sign-up page is reachable and config scrapes cleanly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platforms.grok.protocol_client import (  # noqa: E402
    DEFAULT_IMPERSONATE,
    GrokProtocolError,
)
from platforms.grok.protocol_register import GrokProtocolRegister  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Grok protocol registration edge")
    parser.add_argument("--proxy", default="", help="HTTP/SOCKS proxy URL")
    parser.add_argument(
        "--impersonate",
        default=DEFAULT_IMPERSONATE,
        help="curl_cffi impersonate profile (default chrome131)",
    )
    parser.add_argument(
        "--clearance",
        default="auto",
        choices=["auto", "always", "never"],
        help="FlareSolverr clearance mode",
    )
    parser.add_argument(
        "--flaresolverr",
        default="",
        help="FlareSolverr endpoint, e.g. http://127.0.0.1:8191/v1",
    )
    args = parser.parse_args()

    proxy = (args.proxy or "").strip() or None
    extra = {
        "grok_clearance_mode": args.clearance,
    }
    if args.flaresolverr:
        extra["grok_flaresolverr_url"] = args.flaresolverr
    if args.impersonate:
        extra["grok_cf_impersonate"] = args.impersonate

    print(f"[probe] proxy={proxy or 'direct'} clearance={args.clearance} impersonate={args.impersonate}")

    reg = GrokProtocolRegister(
        proxy=proxy,
        log_fn=lambda m: print(f"  {m}"),
        extra=extra,
    )
    client = None
    try:
        client, cfg = reg._prepare_client()
        print("[ok] warm + fetch_config")
        print(f"     site_key={cfg.site_key}")
        print(f"     action_id={cfg.action_id}")
        print(f"     state_tree_len={len(cfg.state_tree)}")
        print(f"     source={cfg.source}")
        print(f"     profile={client.impersonate}")
        print(f"     ua={client.user_agent[:80]}...")
        cookies = client.export_cookie_pairs()
        names = sorted({n for n, _v, _d in cookies})
        print(f"     cookies={names}")
        return 0
    except GrokProtocolError as exc:
        print(f"[fail] protocol: [{getattr(exc, 'code', '')}] {exc}")
        return 2
    except Exception as exc:
        print(f"[fail] {type(exc).__name__}: {exc}")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
