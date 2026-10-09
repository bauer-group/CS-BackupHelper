"""scripts/e2e.sh helper: seed, delete and check the objects of the s3-source round trip.

Runs inside the backup image, which ships boto3, against the e2e MinIO:

    docker compose ... run --rm -T -e E2E_S3_ACCESS_KEY -e E2E_S3_SECRET_KEY \
        --entrypoint python backup - <seed|delete|check|check-untagged> < scripts/e2e_s3.py

``check`` expects every object back with its body, content headers, user
metadata and tags; ``check-untagged`` the same without tags (a backup whose
credentials could not read them). Prints one line per finding, exits 1 on any.
"""

from __future__ import annotations

import gzip
import os
import sys
from urllib.parse import quote, urlencode

import boto3
from botocore.client import Config

BUCKET = "assets"

# Outline stores attachments of types a browser would render with
# "Content-Disposition: attachment"; the other three headers ride along.
REPORT_HEADERS = {
    "ContentDisposition": 'attachment; filename="report 2026.html"',
    "CacheControl": "private, max-age=600",
    "ContentEncoding": "gzip",
    "ContentLanguage": "de-DE",
}
OBJECTS = {
    "photos/cat.bin": {
        "Body": b"image-bytes",
        "ContentType": "application/octet-stream",
        "Metadata": {},
        "Tags": {"env": "prod", "lang": "c++"},  # "+" must survive the Tagging query string
        "Headers": {},
    },
    "att/report 2026.html": {
        "Body": gzip.compress(b"<h1>report</h1>", mtime=0),
        "ContentType": "text/html",
        "Metadata": {"owner": "e2e"},
        "Tags": {"env": "prod", "path": "a/b c"},
        "Headers": REPORT_HEADERS,
    },
}


def client():
    return boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id=os.environ["E2E_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["E2E_S3_SECRET_KEY"],
        region_name="eu-central-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def seed(c) -> list[str]:
    for key, obj in OBJECTS.items():
        c.put_object(Bucket=BUCKET, Key=key, Body=obj["Body"], ContentType=obj["ContentType"],
                     Metadata=obj["Metadata"], Tagging=urlencode(obj["Tags"], quote_via=quote),
                     **obj["Headers"])
    return []


def delete(c) -> list[str]:
    for key in OBJECTS:
        c.delete_object(Bucket=BUCKET, Key=key)
    return []


def check(c, *, tagged: bool) -> list[str]:
    problems = []
    for key, obj in OBJECTS.items():
        try:
            got = c.get_object(Bucket=BUCKET, Key=key)
        except c.exceptions.NoSuchKey:
            problems.append(f"{key}: missing")
            continue
        expected = {"Body": obj["Body"], "ContentType": obj["ContentType"],
                    "Metadata": obj["Metadata"], **obj["Headers"]}
        actual = {"Body": got["Body"].read(), "ContentType": got.get("ContentType"),
                  "Metadata": got.get("Metadata", {}),
                  **{h: got.get(h) for h in obj["Headers"]}}
        tags = {t["Key"]: t["Value"]
                for t in c.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]}
        expected["Tags"], actual["Tags"] = (obj["Tags"] if tagged else {}), tags
        for field, value in expected.items():
            if actual[field] != value:
                problems.append(f"{key}: {field} is {actual[field]!r}, expected {value!r}")
    return problems


def main() -> int:
    command = sys.argv[1]
    c = client()
    actions = {
        "seed": lambda: seed(c),
        "delete": lambda: delete(c),
        "check": lambda: check(c, tagged=True),
        "check-untagged": lambda: check(c, tagged=False),
    }
    problems = actions[command]()
    for line in problems:
        print(f"FAIL {line}")
    print(f"{command}: {len(OBJECTS)} objects, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
