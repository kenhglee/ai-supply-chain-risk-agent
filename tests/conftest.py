"""Shared test fixtures.

moto's in-memory DynamoDB backend is not safe against concurrent mutation:
transact_write_items deep-copies its tables for rollback while another thread
may be writing. The fenced checkpoint saver writes from LangGraph's background
threads while the action ledger and finalization write from the main thread,
so every moto DynamoDB backend operation is serialized here for the whole test
session. Real DynamoDB needs no such lock and production code has none.
"""
import threading

import pytest

_MOTO_DYNAMODB_LOCK = threading.RLock()
_SERIALIZED = ("put_item", "update_item", "delete_item", "transact_write_items", "get_item", "query", "scan",
               "batch_get_item", "transact_get_items")


@pytest.fixture(autouse=True, scope="session")
def _serialize_moto_dynamodb():
    try:
        from moto.dynamodb.models import DynamoDBBackend
    except ImportError:
        yield
        return
    originals = {name: getattr(DynamoDBBackend, name) for name in _SERIALIZED if hasattr(DynamoDBBackend, name)}

    def locked(fn):
        def wrapper(self, *args, **kwargs):
            with _MOTO_DYNAMODB_LOCK:
                return fn(self, *args, **kwargs)
        return wrapper

    for name, fn in originals.items():
        setattr(DynamoDBBackend, name, locked(fn))
    yield
    for name, fn in originals.items():
        setattr(DynamoDBBackend, name, fn)
