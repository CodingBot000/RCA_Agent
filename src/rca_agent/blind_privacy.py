"""Evaluator-side transforms reduce direct identifiers, not telemetry fingerprinting."""

import hashlib
import hmac
from pathlib import Path

from rca_agent.contracts import ContractError

# Pattern-based sanitization is deliberately limited and is not a PII guarantee.
LOG_MASKS = (
    (r"\b\d{4}[-/]\d{2}[-/]\d{2}(?:[T ][0-9:.]+(?:Z|[+-][0-9:]+)?)?", "[TIME]"),
    (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "[ID]"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]"),
    (r"\b[0-9a-fA-F]{16,}\b", "[ID]"),
    (r"\b\d{10,19}\b", "[NUMBER]"),
)


def shifted_time(key: bytes, source_id: str, original: int, forbidden: set[int]) -> int:
    if len(key) != 32:
        raise ContractError("private selection key must contain 32 bytes")
    for attempt in range(1000):
        digest = hmac.new(key, f"time\0{source_id}\0{attempt}".encode(), hashlib.sha256).digest()
        result = 1_600_000_000 + int.from_bytes(digest[:8], "big") % 200_000_000
        if result != original and result not in forbidden:
            return result
    raise ContractError("unable to find a safe shifted time")


def _pseudonym(key: bytes, case: str, domain: str, value):
    if value is None or value in ("", "0", "null", "None"):
        return value
    data = f"{domain}\0{case}\0{value}".encode()
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:32]


def transform_parquet(
    source: Path, destination: Path, kind: str, shift: int, key: bytes, case: str
):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if kind not in {"metrics", "logs", "traces"}:
        raise ContractError("unknown telemetry type")
    parquet = pq.ParquetFile(source)
    schema = parquet.schema_arrow.remove_metadata()
    required = {
        "metrics": {"time"},
        "logs": {"timestamp", "message"},
        "traces": {"time", "traceID", "spanID", "parentSpanID", "startTime", "startTimeMillis"},
    }[kind]
    if not required <= set(schema.names):
        raise ContractError("telemetry schema is missing privacy-critical fields")
    with pq.ParquetWriter(destination, schema, compression="zstd") as writer:
        for batch in parquet.iter_batches(batch_size=16384):
            columns = []
            for i, field in enumerate(schema):
                array = batch.column(i)
                if (
                    kind == "metrics"
                    and field.name == "time"
                    or kind == "logs"
                    and field.name == "timestamp"
                ):
                    array = pc.add_checked(array, shift)
                elif kind == "logs" and field.name == "message":
                    for pattern, replacement in LOG_MASKS:
                        array = pc.replace_substring_regex(array, pattern, replacement)
                elif kind == "traces" and field.name in {"startTime", "startTimeMillis"}:
                    scale = 1_000_000 if field.name == "startTime" else 1_000
                    array = pc.add_checked(array, shift * scale)
                elif kind == "traces" and field.name == "time":
                    array = pa.array(
                        ["[redacted]" if v is not None else None for v in array.to_pylist()],
                        type=field.type,
                    )
                elif kind == "traces" and field.name in {"traceID", "spanID", "parentSpanID"}:
                    domain = "trace" if field.name == "traceID" else "span"
                    cache = {}
                    values = array.to_pylist()
                    for value in set(values):
                        cache[value] = _pseudonym(key, case, domain, value)
                    array = pa.array([cache[v] for v in values], type=field.type)
                columns.append(array)
            writer.write_batch(pa.RecordBatch.from_arrays(columns, schema=schema))
