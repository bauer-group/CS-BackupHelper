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
