"""Cloud origin, tenant and account identity do not share local cache roots."""

import hashlib
import pytest
from core.capabilities.ref import cloud_issuer, cloud_ref, profile_id, ResourceRef


@pytest.mark.parametrize(
    "other",
    [
        "http://cloud.example/tenant",
        "https://cloud.example/other",
        "https://cloud.example/Tenant",
        "https://cloud.example:8443/tenant",
    ],
)
def test_origin_and_tenant_have_distinct_issuer_and_profile(other):
    base = "https://cloud.example/tenant"
    assert cloud_issuer(base) != cloud_issuer(other)
    assert profile_id(base, "same-user") != profile_id(other, "same-user")


@pytest.mark.parametrize(
    "alias", ["https://CLOUD.EXAMPLE:443/tenant/", " https://cloud.example/tenant "]
)
def test_equivalent_origin_spelling_is_stable(alias):
    assert cloud_issuer(alias) == cloud_issuer("https://cloud.example/tenant")
    assert profile_id(alias, "user") == profile_id("https://cloud.example/tenant", "user")


def test_profile_subject_is_independent_and_reference_roundtrips():
    assert profile_id("https://cloud.example/tenant", "a") != profile_id(
        "https://cloud.example/tenant", "b"
    )
    ref = cloud_ref("https://cloud.example/tenant", "skill", "writing", scope="private")
    assert ResourceRef.parse(str(ref)) == ref


@pytest.mark.parametrize(
    "bad",
    [
        "cloud.example",
        "ftp://cloud.example/tenant",
        "https://user:password@cloud.example",
        "https://cloud.example?token=private",
        "https://cloud.example/#fragment",
        "https://cloud.example:bad",
        "https://cloud.example/space here",
        "https://cloud.example/\\other",
    ],
)
def test_ambiguous_or_credential_bearing_cloud_base_is_rejected(bad):
    with pytest.raises(ValueError):
        profile_id(bad, "user")


def test_new_identity_does_not_alias_or_delete_legacy_cache(tmp_path):
    legacy = "p_" + hashlib.sha256(b"cloud.example\0user").hexdigest()[:10]
    old = tmp_path / legacy / "retained.txt"
    old.parent.mkdir()
    old.write_text("legacy bytes")
    current = profile_id("https://cloud.example", "user")
    assert current != legacy
    assert len(current) == 34
    assert old.read_text() == "legacy bytes"
    assert not (tmp_path / current).exists()
