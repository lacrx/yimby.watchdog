#!/usr/bin/env python3
"""Seed AWS SSM Parameter Store from config.local.yaml.

Reads config.local.yaml, flattens to slash-separated keys, and writes
each value to SSM under the specified prefix. JSON-serializes complex
values (dicts, lists).

Usage:
    python scripts/seed_ssm.py                          # dry-run (default)
    python scripts/seed_ssm.py --write                  # write to SSM
    python scripts/seed_ssm.py --prefix /civic-monitor/ # custom prefix
    python scripts/seed_ssm.py --delete                 # delete all params under prefix
"""

import argparse
import json
import sys
from pathlib import Path

import boto3
import yaml


def flatten(d, prefix="", depth=0):
    """Flatten nested dict to slash-separated keys, max 2 levels.

    Matches config.py's _flatten: top-level keys are sections (identity, feeds),
    second-level keys are parameters. Values deeper than that get JSON-encoded.
    """
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}" if prefix else k
        if isinstance(v, dict) and depth < 1:
            out.update(flatten(v, f"{key}/", depth + 1))
        elif isinstance(v, (dict, list)):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = str(v)
    return out


def main():
    parser = argparse.ArgumentParser(description="Seed SSM Parameter Store from config.local.yaml")
    parser.add_argument("--prefix", default="/civic-monitor/", help="SSM path prefix")
    parser.add_argument("--write", action="store_true", help="Actually write to SSM (default is dry-run)")
    parser.add_argument("--delete", action="store_true", help="Delete all parameters under prefix")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--profile", default=None, help="AWS profile name")
    parser.add_argument("--config", default="config.local.yaml", help="Config file to read")
    args = parser.parse_args()

    prefix = args.prefix.rstrip("/") + "/"

    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    if args.delete:
        ssm = session.client("ssm")
        paginator = ssm.get_paginator("get_parameters_by_path")
        names = []
        for page in paginator.paginate(Path=prefix, Recursive=True):
            names.extend(p["Name"] for p in page["Parameters"])

        if not names:
            print(f"No parameters found under {prefix}")
            return

        print(f"Deleting {len(names)} parameters under {prefix}:")
        for name in names:
            print(f"  {name}")
        if not args.write:
            print("\nDry run — pass --write to delete")
            return
        for i in range(0, len(names), 10):
            ssm.delete_parameters(Names=names[i:i+10])
        print("Deleted.")
        return

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    params = flatten(raw)

    print(f"{'Writing' if args.write else 'Would write'} {len(params)} parameters to {prefix}")
    print()

    ssm = session.client("ssm") if args.write else None

    for key, value in sorted(params.items()):
        full_key = f"{prefix}{key}"
        display_value = value[:80] + "..." if len(value) > 80 else value
        print(f"  {full_key} = {display_value}")

        if args.write:
            ssm.put_parameter(
                Name=full_key,
                Value=value,
                Type="String",
                Overwrite=True,
            )

    if not args.write:
        print(f"\nDry run — pass --write to push {len(params)} parameters to SSM")
    else:
        print(f"\nWrote {len(params)} parameters to SSM under {prefix}")


if __name__ == "__main__":
    main()
