"""Storage abstraction — local filesystem or S3 backend.

Set WATCHDOG_S3_BUCKET env var to enable S3 mode.
When unset, all operations use local filesystem via DATA_DIR.
"""

import json
import os
from pathlib import Path

S3_BUCKET = os.environ.get("WATCHDOG_S3_BUCKET")
_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    return _s3_client


def _data_dir():
    from civic_utils import DATA_DIR
    return DATA_DIR


def is_s3():
    return bool(S3_BUCKET)


def read_text(key):
    """Read a text file by key (relative path like 'oceanside/documents/foo.txt')."""
    if S3_BUCKET:
        try:
            obj = _get_s3().get_object(Bucket=S3_BUCKET, Key=f"raw/{key}")
            return obj["Body"].read().decode("utf-8")
        except _get_s3().exceptions.NoSuchKey:
            return None
    else:
        path = _data_dir() / key
        return path.read_text() if path.exists() else None


def read_bytes(key):
    """Read binary file by key."""
    if S3_BUCKET:
        try:
            obj = _get_s3().get_object(Bucket=S3_BUCKET, Key=f"raw/{key}")
            return obj["Body"].read()
        except _get_s3().exceptions.NoSuchKey:
            return None
    else:
        path = _data_dir() / key
        return path.read_bytes() if path.exists() else None


def write_text(key, content, prefix="raw"):
    """Write text content to key."""
    if S3_BUCKET:
        _get_s3().put_object(Bucket=S3_BUCKET, Key=f"{prefix}/{key}", Body=content.encode("utf-8"))
    else:
        path = _data_dir() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def write_bytes(key, content, prefix="raw"):
    """Write binary content to key."""
    if S3_BUCKET:
        _get_s3().put_object(Bucket=S3_BUCKET, Key=f"{prefix}/{key}", Body=content)
    else:
        path = _data_dir() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def write_json(key, data, prefix="raw"):
    """Write JSON-serializable data to key."""
    write_text(key, json.dumps(data, indent=2, default=str), prefix=prefix)


def read_json(key, prefix="raw"):
    """Read JSON from key. Returns None if not found."""
    if S3_BUCKET:
        try:
            obj = _get_s3().get_object(Bucket=S3_BUCKET, Key=f"{prefix}/{key}")
            return json.loads(obj["Body"].read().decode("utf-8"))
        except Exception:
            return None
    else:
        path = _data_dir() / key
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None


def exists(key, prefix="raw"):
    """Check if key exists."""
    if S3_BUCKET:
        try:
            _get_s3().head_object(Bucket=S3_BUCKET, Key=f"{prefix}/{key}")
            return True
        except Exception:
            return False
    else:
        return (_data_dir() / key).exists()


def list_keys(prefix_key, s3_prefix="raw"):
    """List keys under a prefix. Returns relative key strings."""
    if S3_BUCKET:
        full_prefix = f"{s3_prefix}/{prefix_key}"
        paginator = _get_s3().get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][len(f"{s3_prefix}/"):])
        return keys
    else:
        base = _data_dir() / prefix_key
        if not base.exists():
            return []
        return [str(f.relative_to(_data_dir())) for f in base.rglob("*") if f.is_file()]


def sync_dir_to_s3(local_dir, s3_prefix):
    """Sync a local directory to S3. Used after scraping in Lambda."""
    if not S3_BUCKET:
        return
    client = _get_s3()
    local_dir = Path(local_dir)
    for f in local_dir.rglob("*"):
        if f.is_file():
            key = f"{s3_prefix}/{f.relative_to(local_dir)}"
            client.upload_file(str(f), S3_BUCKET, key)


def sync_s3_to_dir(s3_prefix, local_dir, exclude_exts=None):
    """Sync S3 prefix to local directory. Used before local extraction."""
    if not S3_BUCKET:
        return
    client = _get_s3()
    local_dir = Path(local_dir)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=s3_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if exclude_exts and any(key.endswith(ext) for ext in exclude_exts):
                continue
            rel = key[len(s3_prefix):].lstrip("/")
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(S3_BUCKET, key, str(dest))
