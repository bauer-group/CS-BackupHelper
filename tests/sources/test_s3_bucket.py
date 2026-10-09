"""Tests for the S3-bucket source — full mirror WITH per-object metadata."""

import json
import tarfile

import boto3
import pytest
from moto import mock_aws

from backuphelper.sources.s3_bucket import S3BucketSource

REGION = "eu-central-1"


def _spec(bucket):
    return {"type": "s3", "bucket": bucket, "region": REGION,
            "access_key": "test", "secret_key": "test", "name": "attachments"}


def _client():
    return boto3.client("s3", region_name=REGION, aws_access_key_id="test",
                        aws_secret_access_key="test")


def _read_tar_json(archive, member):
    with tarfile.open(archive, "r:gz") as tar:
        return json.loads(tar.extractfile(member).read())


@mock_aws
def test_mirrors_objects_and_captures_per_object_metadata(tmp_path):
    c = _client()
    c.create_bucket(Bucket="src", CreateBucketConfiguration={"LocationConstraint": REGION})
    c.put_object(Bucket="src", Key="docs/a.txt", Body=b"hello",
                 ContentType="text/plain", Metadata={"owner": "alice"},
                 Tagging="env=prod&tier=1")

    comps = S3BucketSource(_spec("src")).produce(tmp_path)
    assert len(comps) == 1
    c0 = comps[0]
    assert c0.kind == "s3" and c0.error is None

    with tarfile.open(c0.path, "r:gz") as tar:
        names = tar.getnames()
    assert "objects/docs/a.txt" in names
    assert "metadata.json" in names

    meta = _read_tar_json(c0.path, "metadata.json")
    obj = next(o for o in meta["objects"] if o["key"] == "docs/a.txt")
    assert obj["content_type"] == "text/plain"
    assert obj["metadata"] == {"owner": "alice"}
    assert obj["tags"] == {"env": "prod", "tier": "1"}


@mock_aws
def test_restore_reuploads_with_content_type_metadata_and_tags(tmp_path):
    c = _client()
    c.create_bucket(Bucket="src", CreateBucketConfiguration={"LocationConstraint": REGION})
    c.put_object(Bucket="src", Key="x.bin", Body=b"data", ContentType="application/octet-stream",
                 Metadata={"k": "v"}, Tagging="a=b")

    produced = S3BucketSource(_spec("src")).produce(tmp_path)[0]

    # Extract the component tar (as the engine would) and restore into a new bucket.
    extracted = tmp_path / "extracted"
    with tarfile.open(produced.path, "r:gz") as tar:
        tar.extractall(extracted, filter="data")
    c.create_bucket(Bucket="dst", CreateBucketConfiguration={"LocationConstraint": REGION})
    S3BucketSource(_spec("dst")).restore(extracted)

    head = c.head_object(Bucket="dst", Key="x.bin")
    assert head["ContentType"] == "application/octet-stream"
    assert head["Metadata"] == {"k": "v"}
    tags = {t["Key"]: t["Value"] for t in c.get_object_tagging(Bucket="dst", Key="x.bin")["TagSet"]}
    assert tags == {"a": "b"}
    assert c.get_object(Bucket="dst", Key="x.bin")["Body"].read() == b"data"


@mock_aws
def test_empty_bucket_produces_component_with_zero_objects(tmp_path):
    c = _client()
    c.create_bucket(Bucket="empty", CreateBucketConfiguration={"LocationConstraint": REGION})
    comp = S3BucketSource(_spec("empty")).produce(tmp_path)[0]
    meta = _read_tar_json(comp.path, "metadata.json")
    assert meta["object_count"] == 0


# Outline serves attachments of unsafe types with "Content-Disposition: attachment"
# so a browser downloads them instead of rendering them: losing the header on a
# restore turns an uploaded HTML/SVG file into a page rendered on Outline's origin.
CONTENT_HEADERS = {
    "ContentDisposition": 'attachment; filename="report 2026.html"',
    "CacheControl": "private, max-age=600",
    "ContentEncoding": "identity",
    "ContentLanguage": "de-DE",
}


@mock_aws
def test_mirror_captures_the_content_headers(tmp_path):
    c = _client()
    c.create_bucket(Bucket="src", CreateBucketConfiguration={"LocationConstraint": REGION})
    c.put_object(Bucket="src", Key="att/report.html", Body=b"<h1>x</h1>",
                 ContentType="text/html", **CONTENT_HEADERS)
    c.put_object(Bucket="src", Key="plain.txt", Body=b"x")

    comp = S3BucketSource(_spec("src")).produce(tmp_path)[0]

    objects = {o["key"]: o for o in _read_tar_json(comp.path, "metadata.json")["objects"]}
    report = objects["att/report.html"]
    assert report["content_disposition"] == CONTENT_HEADERS["ContentDisposition"]
    assert report["cache_control"] == CONTENT_HEADERS["CacheControl"]
    assert report["content_encoding"] == CONTENT_HEADERS["ContentEncoding"]
    assert report["content_language"] == CONTENT_HEADERS["ContentLanguage"]
    # An object without the headers records them as absent, not as empty strings.
    assert objects["plain.txt"]["content_disposition"] is None


@mock_aws
def test_restore_reapplies_the_content_headers(tmp_path):
    c = _client()
    c.create_bucket(Bucket="src", CreateBucketConfiguration={"LocationConstraint": REGION})
    c.put_object(Bucket="src", Key="att/report.html", Body=b"<h1>x</h1>",
                 ContentType="text/html", **CONTENT_HEADERS)
    produced = S3BucketSource(_spec("src")).produce(tmp_path)[0]
    extracted = tmp_path / "extracted"
    with tarfile.open(produced.path, "r:gz") as tar:
        tar.extractall(extracted, filter="data")
    c.create_bucket(Bucket="dst", CreateBucketConfiguration={"LocationConstraint": REGION})

    S3BucketSource(_spec("dst")).restore(extracted)

    head = c.head_object(Bucket="dst", Key="att/report.html")
    assert head["ContentType"] == "text/html"
    for header, value in CONTENT_HEADERS.items():
        assert head[header] == value, header


@mock_aws
def test_restore_of_a_snapshot_without_content_headers(tmp_path):
    # metadata.json written before the headers were captured has no such keys.
    staged = tmp_path / "staged"
    (staged / "objects").mkdir(parents=True)
    (staged / "objects" / "a.txt").write_bytes(b"old")
    (staged / "metadata.json").write_text(json.dumps({"objects": [
        {"key": "a.txt", "content_type": "text/plain", "metadata": {}, "tags": {}}]}))
    c = _client()
    c.create_bucket(Bucket="dst", CreateBucketConfiguration={"LocationConstraint": REGION})

    S3BucketSource(_spec("dst")).restore(staged)

    head = c.head_object(Bucket="dst", Key="a.txt")
    assert head["ContentType"] == "text/plain"
    assert "ContentDisposition" not in head
