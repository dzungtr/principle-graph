// Principle Graph prototype schema (Neo4j Community 5.x)
// Apply with cypher-shell or the Neo4j driver during scaffold startup.
// The script is idempotent: IF NOT EXISTS is safe on repeated startup.

// An Entity is identified by its canonical name within its type.
CREATE CONSTRAINT entity_name_type_unique IF NOT EXISTS
FOR (entity:Entity)
REQUIRE (entity.name, entity.type) IS UNIQUE;

// voyage-3 returns 1024-dimensional vectors. Similarity is cosine because
// direction, rather than vector magnitude, is the relevant signal.
CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
FOR (entity:Entity)
ON (entity.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 1024,
    `vector.similarity_function`: 'cosine'
  }
};

// Ledger rows (ADR-0002): append-only per-extraction facts. One :ExtractionEvent per
// accepted extraction, identified by (subject, relation, object, source_ref). Neo4j
// Community 5.x cannot express composite uniqueness across relationship endpoints, so
// row identity is enforced by the write layer's pattern MERGE. This index supports
// provenance lookups by source reference.
CREATE RANGE INDEX extraction_event_source_ref IF NOT EXISTS
FOR (event:ExtractionEvent)
ON (event.source_ref);
