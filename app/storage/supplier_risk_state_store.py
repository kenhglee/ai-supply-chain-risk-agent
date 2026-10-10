"""Supplier risk state persistence for the RSS pipeline.

One row per (supplier, risk_type) holding the current risk level, so the
pipeline can detect new alerts, escalations, suppressions and downgrades.
Not to be confused with risk_state_store.py, which is the GitHub decision
ledger.
"""
import csv
import os
from pathlib import Path

import boto3


class RiskStateStore:
    def get(self, supplier: str, risk_type: str) -> dict | None:
        raise NotImplementedError

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        raise NotImplementedError

    def list_all(self) -> list[dict]:
        """Current state: one row per (supplier, risk_type)."""
        raise NotImplementedError

    def flush(self) -> None:
        pass


class CsvRiskStateStore(RiskStateStore):
    def __init__(self, path: Path):
        self.path = path
        self.state = self._load()

    def _load(self) -> dict[tuple[str, str], dict]:
        if not self.path.exists():
            return {}

        state = {}
        with open(self.path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                supplier = (row.get("supplier") or "").strip()
                risk_type = (row.get("risk_type") or "").strip()
                if supplier and risk_type:
                    state[(supplier, risk_type)] = row
        return state

    def get(self, supplier: str, risk_type: str) -> dict | None:
        return self.state.get((supplier, risk_type))

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        self.state[(supplier, risk_type)] = row

    def list_all(self) -> list[dict]:
        return list(self.state.values())

    def flush(self) -> None:
        fieldnames = [
            "supplier",
            "risk_type",
            "current_risk_level",
            "last_headline",
            "last_seen_at",
        ]
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in self.state.values():
                writer.writerow(row)


class DynamoRiskStateStore(RiskStateStore):
    def __init__(self, table_name: str, region_name: str | None = None):
        self.dynamodb = boto3.resource(
            "dynamodb",
            region_name=region_name or os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )
        self.table = self.dynamodb.Table(table_name)

    @staticmethod
    def _pk(supplier: str, risk_type: str) -> str:
        return f"{supplier}#{risk_type}"

    def get(self, supplier: str, risk_type: str) -> dict | None:
        response = self.table.get_item(
            Key={"pk": self._pk(supplier, risk_type)}
        )
        return response.get("Item")

    def put(self, supplier: str, risk_type: str, row: dict) -> None:
        item = dict(row)
        item["pk"] = self._pk(supplier, risk_type)
        item["supplier"] = supplier
        item["risk_type"] = risk_type

        self.table.put_item(Item=item)

        # Read-after-write kept from the original implementation; result unused.
        self.table.get_item(Key={"pk": item["pk"]})

    def list_all(self) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return items

    def flush(self) -> None:
        pass


def get_risk_store() -> RiskStateStore:
    backend = os.getenv("RISK_STATE_BACKEND", "csv").lower()

    if backend == "csv":
        path = Path(os.getenv("RISK_STATE_FILE", "risk_state.csv"))
        return CsvRiskStateStore(path)

    if backend == "dynamodb":
        return DynamoRiskStateStore(
            table_name=os.getenv("RISK_STATE_TABLE", "supplier_risk_state"),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
        )

    raise ValueError(f"Unsupported RISK_STATE_BACKEND: {backend}")
