"""Read and write Parquet files in the R2 data lake.

Keys follow a Hive-style layout so DuckDB and dbt can read partitions directly:
    bronze/bts_ontime/year=2024/month=01/data.parquet
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config
from botocore.exceptions import ClientError

from flightops.config import R2Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

Layer = Literal["bronze", "silver", "gold", "quarantine"]


def lake_key(layer: Layer, dataset: str, filename: str = "data.parquet", **partitions: str) -> str:
    """Build an object key, e.g. lake_key("bronze", "bts_ontime", year="2024", month="01")."""
    parts = [layer, dataset, *(f"{name}={value}" for name, value in partitions.items()), filename]
    return "/".join(parts)


class LakeStorage:
    """Thin wrapper over an S3-compatible client, scoped to one bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings: R2Settings | None = None) -> LakeStorage:
        settings = settings or R2Settings()
        client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key.get_secret_value(),
            region_name="auto",
            config=Config(
                retries={"max_attempts": 5, "mode": "standard"},
                # Enough connections for read_parquet_many's parallel reads.
                max_pool_connections=32,
                # boto3 >= 1.36 adds checksum headers by default that R2 may reject;
                # only send them when an operation requires it.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(client, settings.bucket)

    def write_parquet(self, key: str, table: pa.Table, *, overwrite: bool = False) -> None:
        """Write a table. Refuses to replace an existing object unless overwrite=True,
        so bronze (raw) data can't be modified by accident."""
        if not overwrite and self.exists(key):
            raise FileExistsError(f"s3://{self.bucket}/{key} already exists")
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="zstd")
        self._client.put_object(Bucket=self.bucket, Key=key, Body=buffer.getvalue())

    def read_parquet(self, key: str, columns: list[str] | None = None) -> pa.Table:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return pq.read_table(io.BytesIO(response["Body"].read()), columns=columns)

    def read_parquet_many(
        self, keys: list[str], columns: list[str] | None = None, *, workers: int = 16
    ) -> pa.Table:
        """Read many small files in parallel and stack them, in the order of `keys`.

        Each read is one network round trip, so fetching a day of 5-minute snapshots one at a
        time takes minutes; boto3 clients are safe to share between threads.
        """
        with ThreadPoolExecutor(max_workers=workers) as pool:
            tables = list(pool.map(lambda key: self.read_parquet(key, columns), keys))
        return pa.concat_tables(tables)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []) if "Key" in obj)
        return keys

    def delete(self, keys: list[str]) -> None:
        """Delete objects (in batches of 1,000, the S3 limit per request)."""
        for start in range(0, len(keys), 1000):
            batch: list[ObjectIdentifierTypeDef] = [{"Key": k} for k in keys[start : start + 1000]]
            response = self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
            if response.get("Errors"):
                raise RuntimeError(f"failed to delete: {response['Errors']}")
