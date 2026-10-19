#!/usr/bin/env python3
"""Verify a real embedded-vector dependency upgrade using disposable memories.

Run with --old-python and --new-python, each with its respective Milvus runtime.
The --seed/--verify worker modes also permit testing the copied fixture on another OS.
"""
import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile

COLLECTION = "memory_upgrade_probe"
ORIGINAL = [
    {
        "id": "memory-alice",
        "vectors": [1.0, 0.0, 0.0, 0.0],
        "metadata": {"user_id": "alice", "data": "保留原有中文记忆", "nested": {"verified": True}},
    },
    {
        "id": "memory-bob",
        "vectors": [0.0, 1.0, 0.0, 0.0],
        "metadata": {"user_id": "bob", "data": "independent user memory"},
    },
]
NEW = {
    "id": "memory-new",
    "vectors": [0.0, 0.0, 1.0, 0.0],
    "metadata": {"user_id": "alice", "data": "new memory after upgrade"},
}


def open_client(root):
    from pymilvus import MilvusClient

    return MilvusClient(uri=str(root / "milvus.db"))


def close_client(client, root):
    from milvus_lite.server_manager import server_manager_instance

    try:
        client.close()
    finally:
        server_manager_instance.release_server(str(root / "milvus.db"))


def seed(root):
    from pymilvus import CollectionSchema, DataType, FieldSchema

    if root.exists():
        raise RuntimeError("Seed destination must not exist")
    root.mkdir(parents=True)
    client = open_client(root)
    try:
        schema = CollectionSchema(
            [
                FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=512),
                FieldSchema(name="vectors", dtype=DataType.FLOAT_VECTOR, dim=4),
                FieldSchema(name="metadata", dtype=DataType.JSON),
            ],
            enable_dynamic_field=False,
        )
        indexes = client.prepare_index_params()
        indexes.add_index(field_name="vectors", index_type="FLAT", metric_type="COSINE")
        client.create_collection(collection_name=COLLECTION, schema=schema, index_params=indexes)
        client.insert(collection_name=COLLECTION, data=ORIGINAL)
        client.flush(collection_name=COLLECTION)
    finally:
        close_client(client, root)
    (root / "upgrade-fixture.json").write_text(json.dumps(ORIGINAL), encoding="utf-8")


def verify(root):
    # Refuse arbitrary stores. This marker is created only by the seed worker.
    if json.loads((root / "upgrade-fixture.json").read_text(encoding="utf-8")) != ORIGINAL:
        raise RuntimeError("Not a vector upgrade test fixture")
    client = open_client(root)
    try:
        client.load_collection(collection_name=COLLECTION)
        rows = client.query(
            collection_name=COLLECTION, filter='id != ""', output_fields=["id", "metadata"]
        )
        expected = {r["id"]: r["metadata"] for r in ORIGINAL}
        if {r["id"]: r["metadata"] for r in rows} != expected:
            raise RuntimeError("Upgrade changed existing memory ids or metadata")
        # Rebuilding the index writes the already-existing manifest.
        client.release_collection(collection_name=COLLECTION)
        for index in client.list_indexes(collection_name=COLLECTION):
            client.drop_index(collection_name=COLLECTION, index_name=index)
        indexes = client.prepare_index_params()
        indexes.add_index(
            field_name="vectors",
            index_type="FLAT",
            metric_type="COSINE",
            index_name="upgrade_index",
        )
        client.create_index(collection_name=COLLECTION, index_params=indexes)
        client.load_collection(collection_name=COLLECTION)
        client.insert(collection_name=COLLECTION, data=[NEW])
        client.flush(collection_name=COLLECTION)
    finally:
        close_client(client, root)
    client = open_client(root)
    try:
        client.load_collection(collection_name=COLLECTION)
        rows = client.query(
            collection_name=COLLECTION, filter='id != ""', output_fields=["id", "metadata"]
        )
        expected[NEW["id"]] = NEW["metadata"]
        if {r["id"]: r["metadata"] for r in rows} != expected:
            raise RuntimeError("Append/reopen lost existing or new memories")
        result = client.search(
            collection_name=COLLECTION,
            data=[[1.0, 0.0, 0.0, 0.0]],
            anns_field="vectors",
            filter='metadata["user_id"] == "alice"',
            limit=2,
            output_fields=["metadata"],
            search_params={"metric_type": "COSINE"},
        )
        if not result[0] or result[0][0]["id"] != "memory-alice":
            raise RuntimeError("Existing memory no longer searchable")
        if any(hit["entity"]["metadata"]["user_id"] != "alice" for hit in result[0]):
            raise RuntimeError("User filter leaked another user's memory")
    finally:
        close_client(client, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-python")
    parser.add_argument("--new-python")
    worker = parser.add_mutually_exclusive_group()
    worker.add_argument("--seed", type=Path)
    worker.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.seed or args.verify:
        (seed if args.seed else verify)(args.seed or args.verify)
        print(
            json.dumps(
                {
                    "ok": True,
                    "phase": "seed" if args.seed else "verify",
                    "milvus_lite": importlib.metadata.version("milvus-lite"),
                }
            )
        )
        return
    if not args.old_python or not args.new_python:
        parser.error("--old-python and --new-python are required")
    with tempfile.TemporaryDirectory(prefix="vector-upgrade-e2e-") as folder:
        root = str(Path(folder) / "fixture")
        for python, phase in ((args.old_python, "--seed"), (args.new_python, "--verify")):
            subprocess.run(
                [python, str(Path(__file__).resolve()), phase, root], check=True, timeout=90
            )
    print(json.dumps({"ok": True, "preserved_memories": 2, "appended_memories": 1}))


if __name__ == "__main__":
    main()
