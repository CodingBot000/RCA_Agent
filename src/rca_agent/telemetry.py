"""Read-only, case-scoped telemetry queries and evidence registry."""

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from rca_agent.contracts import ContractError, RCAInput, RCAResult
from rca_agent.phase1 import CASE_ID_PATTERN, load_task

MAX_RESULTS = 100


@dataclass(frozen=True)
class QueryResponse:
    status: str
    call_id: str
    evidence_id: str | None = None
    records: tuple[dict[str, Any], ...] = ()
    message: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    case_id: str
    call_id: str
    source: str
    content: str


class EvidenceRegistry:
    def __init__(self, case_id: str):
        self.case_id = case_id
        self._records: dict[str, EvidenceRecord] = {}
        self._capability = object()

    def register(self, record: EvidenceRecord, *, status: str, _proof: object = None) -> None:
        if status != "ok" or _proof is not self._capability:
            raise ContractError("only successful queries may create evidence")
        if record.case_id != self.case_id:
            raise ContractError("cross-case evidence")
        if not all(
            isinstance(x, str) and x.strip()
            for x in (record.evidence_id, record.call_id, record.source, record.content)
        ):
            raise ContractError("evidence fields must be nonempty")
        if record.evidence_id in self._records:
            raise ContractError("duplicate evidence ID")
        self._records[record.evidence_id] = record

    def validate_result(self, raw: dict, task: RCAInput) -> RCAResult:
        if task.case_id != self.case_id:
            raise ContractError("cross-case task")
        existing = {e.id for e in task.evidence}
        if existing & self._records.keys():
            raise ContractError("duplicate input evidence ID")
        data = {
            "case_id": task.case_id,
            "candidates": list(task.candidates),
            "evidence": [{"id": e.id, "content": e.content} for e in task.evidence]
            + [{"id": e.evidence_id, "content": e.content} for e in self._records.values()],
        }
        return RCAResult.parse(raw, RCAInput.parse(data))

    def finalize(self, raw: dict, task: RCAInput) -> tuple[str, RCAResult | None]:
        """Return an explicit failure when no successful query supplied evidence."""
        if not self._records:
            return "insufficient_evidence", None
        try:
            result = self.validate_result(raw, task)
            referenced = {item for rank in result.ranking for item in rank.evidence_ids}
            if not referenced.intersection(self._records):
                return "insufficient_evidence", None
            return "ok", result
        except (ContractError, TypeError, KeyError):
            return "error", None


class TelemetryTools:
    def __init__(self, blind_root: Path, case_id: str, registry: EvidenceRegistry):
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise ContractError("invalid case ID")
        if blind_root.is_symlink() or not blind_root.is_dir():
            raise ContractError("invalid blind root")
        case = blind_root / case_id
        if case.is_symlink() or not case.is_dir() or registry.case_id != case_id:
            raise ContractError("invalid case or registry")
        for name in ("task.json", "metrics.parquet", "logs.parquet", "traces.parquet"):
            path = case / name
            if path.is_symlink():
                raise ContractError("symlink not allowed")
        task = load_task(case)
        if task.case_id != case_id:
            raise ContractError("case ID mismatch")
        self.case, self.task, self.registry = case, task, registry
        self.services = {x.split("_", 1)[0] for x in task.candidates}

    def query_metrics(
        self, case_id: str, service: str, start: int, end: int, limit: int, indicator: str
    ) -> QueryResponse:
        return self._query("metrics", case_id, service, start, end, limit, indicator)

    def query_logs(
        self, case_id: str, service: str, start: int, end: int, limit: int
    ) -> QueryResponse:
        return self._query("logs", case_id, service, start, end, limit, None)

    def query_traces(
        self, case_id: str, service: str, start: int, end: int, limit: int
    ) -> QueryResponse:
        return self._query("traces", case_id, service, start, end, limit, None)

    def _query(self, kind, case_id, service, start, end, limit, indicator):
        call = uuid.uuid4().hex
        try:
            if case_id != self.task.case_id or service not in self.services:
                raise ContractError("unknown case or service")
            if (
                any(type(x) is not int for x in (start, end, limit))
                or not 1 <= limit <= MAX_RESULTS
                or start >= end
            ):
                raise ContractError("invalid range or result limit")
            metrics_path = self.case / "metrics.parquet"
            if metrics_path.is_symlink():
                raise ContractError("symlink not allowed")
            table = pq.read_table(metrics_path, columns=["time"])
            bounds = pc.min_max(table["time"]).as_py()
            if bounds["min"] is None or start < bounds["min"] or end > bounds["max"] + 1:
                raise ContractError("time range outside case")
            path = self.case / f"{kind}.parquet"
            if path.is_symlink():
                raise ContractError("symlink not allowed")
            if not path.is_file():
                return QueryResponse("unavailable", call, message="telemetry unavailable")
            dataset = ds.dataset(path, format="parquet")
            if kind == "metrics":
                if (
                    indicator not in self.task.candidates
                    or not indicator.startswith(service + "_")
                    or indicator not in dataset.schema.names
                ):
                    raise ContractError("unknown indicator")
                time, svc, cols = "time", None, ["time", indicator]
            elif kind == "logs":
                time, svc, cols = (
                    "timestamp",
                    "container_name",
                    ["timestamp", "container_name", "message"],
                )
            else:
                time, svc = "startTime", "serviceName"
                cols = [
                    name
                    for name in (
                        "startTime",
                        "serviceName",
                        "operationName",
                        "duration",
                        "statusCode",
                        "traceID",
                        "spanID",
                        "parentSpanID",
                    )
                    if name in dataset.schema.names
                ]
            if (
                time not in dataset.schema.names
                or (svc and svc not in dataset.schema.names)
                or not set(cols) <= set(dataset.schema.names)
            ):
                raise ContractError("unsupported telemetry schema")
            scale = 1_000_000 if kind == "traces" else 1
            filt = (ds.field(time) >= start * scale) & (ds.field(time) < end * scale)
            if svc:
                filt &= ds.field(svc) == service
            scanner = dataset.scanner(columns=cols, filter=filt, batch_size=limit + 1)
            rows = []
            for batch in scanner.to_batches():
                rows.extend(batch.slice(0, limit + 1 - len(rows)).to_pylist())
                if len(rows) > limit:
                    break
            if not rows:
                return QueryResponse("empty", call, message="no matching telemetry")
            truncated = len(rows) > limit
            rows = rows[:limit]

            def clean(v):
                return None if isinstance(v, float) and not math.isfinite(v) else v

            rows = [{k: clean(v) for k, v in row.items()} for row in rows]
            content = json.dumps(
                {
                    "source": kind,
                    "call_id": call,
                    "service": service,
                    "start": start,
                    "end": end,
                    "records": rows,
                    "truncated": truncated,
                },
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            evidence_id = hashlib.sha256(content.encode()).hexdigest()
            self.registry.register(
                EvidenceRecord(evidence_id, case_id, call, kind, content),
                status="ok",
                _proof=self.registry._capability,
            )
            return QueryResponse("ok", call, evidence_id, tuple(rows), truncated=truncated)
        except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
            return QueryResponse("error", call, message=str(exc))
