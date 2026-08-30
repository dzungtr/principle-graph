# Confidence aggregation and repeat-extraction policy

Status: accepted for the prototype (2026-08-03)

> Superseded in part by [ADR-0002](adr/0002-ledger-two-layer-schema.md) (ledger schema): evidence and provenance now live on `:ExtractionEvent` ledger rows rather than edge property lists; the complement formula is unchanged but computed over rows; repeat behavior gains explicit keep-first/refresh modes. The Mode-2 rejected-deltas section is unchanged.

## Repeat-edge identity

A repeated extraction for the same `(subject Entity, relation, object Entity)` updates a
**single relationship** in place. The relationship identity is the endpoint pair plus the
normalized Neo4j relationship type; it is not a separate relationship instance per source.
This keeps fan-out deterministic while retaining the evidence needed to explain the aggregate.

## Confidence aggregation

Each extraction event supplies a confidence in the inclusive range `0.0` to `1.0`. On the
first accepted extraction, the edge confidence equals that event confidence. On a repeat, the
aggregate uses the independent-evidence complement formula:

```
aggregate = 1 - (1 - previous) * (1 - event)
```

The result is clamped to `[0.0, 1.0]`. This is monotonic, gives diminishing returns to repeated
support, and avoids treating corroborating extractions as fully redundant. Events from the
same source and `source_ref` are not counted as independent corroboration: they update the
edge's evidence/provenance but do not raise confidence a second time.

An extraction's confidence is not a truth score. It expresses confidence in that event only;
the aggregate expresses accumulated support and must not exceed 1.0.

## Evidence and provenance retention

Every accepted event appends its paraphrased `evidence` and `source_ref` to the relationship's
provenance log. `scope_conditions` are retained per evidence item (or merged without dropping
a qualification); a later extraction may not erase an earlier condition. The persisted schema's
single-value fields remain compatible with the prototype by storing the latest non-empty scope
condition and source reference, while the evidence log is the authoritative audit trail when
multiple sources are present.

## Mode-2 rejected deltas

A candidate rejected at the Mode-2 review checkpoint is **never committed** to the graph and
never contributes confidence. It is retained in the task's review/audit record as "considered and rejected", including the original candidate, evidence, scope conditions, source reference,
rejection reason, and reviewer decision. Rejected records are not returned as graph edges during
fan-out. They may be inspected later as provenance, but re-approval requires an explicit new
review decision. In the ingest command the review/audit record is the local JSONL rejected
log (default `.pg/rejected.jsonl`, env-overridable) — rejected records are not stored in
the graph.

This policy applies to the prototype's single-tenant graph and does not define conflict
resolution between high-confidence claims.
