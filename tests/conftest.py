from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from flightops.storage import LakeStorage


@pytest.fixture
def lake(monkeypatch: pytest.MonkeyPatch) -> Iterator[LakeStorage]:
    """A LakeStorage backed by moto's in-memory S3, so tests never touch real R2."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-lake")
        yield LakeStorage(client, "test-lake")
