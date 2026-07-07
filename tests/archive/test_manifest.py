"""Tests for the schema-versioned, self-describing manifest + sidecar."""

import json

from backuphelper.archive.manifest import (
    Component,
    Manifest,
    read_manifest,
    sidecar_path,
    write_manifest,
)


def test_component_defaults():
    c = Component(name="database", kind="postgres", size=1234, sha256="deadbeef")
    assert c.error is None
    assert c.metadata == {}


def test_build_sums_total_bytes_and_stamps_schema_version():
    comps = [
        Component(name="db", kind="postgres", size=100, sha256="a"),
        Component(name="uploads", kind="filesystem", size=250, sha256="b"),
    ]
    m = Manifest.build(snapshot_id="2026-07-06_03-00-00", instance_name="iam",
                       components=comps, created_at="2026-07-06T03:00:00Z")
    assert m.schema_version == 1
    assert m.total_bytes == 350
    assert m.instance_name == "iam"


def test_write_read_roundtrip_preserves_components_and_extra(tmp_path):
    comps = [Component(name="records", kind="nocodb", size=9, sha256="c",
                       metadata={"records_count": 42})]
    m = Manifest.build(snapshot_id="s1", instance_name="noco", components=comps,
                       created_at="2026-07-06T03:00:00Z", bases_count=3)
    p = tmp_path / "s1.manifest.json"
    write_manifest(m, p)
    back = read_manifest(p)
    assert back.components[0].metadata == {"records_count": 42}
    assert back.model_extra.get("bases_count") == 3


def test_written_manifest_is_indented_json(tmp_path):
    m = Manifest.build(snapshot_id="s", instance_name="i", components=[],
                       created_at="2026-07-06T03:00:00Z")
    p = tmp_path / "s.manifest.json"
    write_manifest(m, p)
    text = p.read_text()
    assert json.loads(text)["snapshot_id"] == "s"
    assert "\n" in text  # indented, not one line


def test_sidecar_path_naming(tmp_path):
    assert sidecar_path(tmp_path, "2026-07-06_03-00-00") == tmp_path / "2026-07-06_03-00-00.manifest.json"
