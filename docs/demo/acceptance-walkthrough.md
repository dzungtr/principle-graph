# Principle Graph demo acceptance walkthrough

This document is the executable acceptance bar for the working-prototype demo.
It deliberately uses a small, versioned Markdown source so that the demo is
repeatable without downloading a book or depending on a changing web page.

## Prerequisites

- Run the local Neo4j Community 5.x service and apply the prototype schema.
- Configure the Anthropic and Voyage credentials required by the CLI.
- Start from an empty graph (or clear the graph before the run).
- Use Python 3.12 and the repository's documented `pg` entry point.

## Source and resource bound

Ingest exactly [`demo-source.md`](demo-source.md), one 120-word Markdown source
with three paragraphs. Do not ingest the full book or add background knowledge.
The source is intentionally short enough for one extraction request and one
review checkpoint.

The run must complete in **10 minutes** on a developer laptop, excluding the
initial container image pull. It may make at most **10 Claude extraction
requests** (one per chunk; the expected source produces no more than three)
and **10 embedding requests**. The demo must report request counts and elapsed
time. A failed request may be retried once, but retries count toward the limit.

## Walkthrough

From the repository root:

```sh
pg ingest docs/demo/demo-source.md
```

The command must process chunks sequentially, show a Mode-2 graph delta, and
pause for the review decision. Approve the supported relationships and reject
any candidate not evidenced by the source. Then run:

```sh
pg query "interest rates are rising"
```

Save the terminal output, including the review decision, commit result, query
result, request counts, and elapsed time, as the end-to-end demo transcript.
The end-to-end run ticket owns the transcript; this document defines what it
must contain and how it is judged.

## Acceptance bar

The demo passes only when all of the following are true:

1. **Ingestion traceability:** every committed edge cites a source reference
   pointing to `demo-source.md` and a source chunk; chunks are processed in
   source order.
2. **Evidence discipline:** committed entities and relationships are supported
   by the source. The output contains no invented named entities, numerical
   claims, or causal relationships that are absent from the source.
3. **Review behavior:** Mode 2 presents the assembled delta before commit and
   the committed graph contains only approved items. Rejected items are visible
   in the review transcript and are not committed.
4. **Fan-out correctness:** the query returns a ranked list with at least these
   two directions (wording may vary):
   - higher interest rates → more expensive borrowing → reduced spending or
     business investment → reduced demand → less upward pressure on prices;
   - supply disruptions can raise prices despite weak demand, as a qualification
     or counter-direction.
5. **Ranking and metadata:** each returned direction includes a confidence,
   source reference, and scope condition when one is present. The demand path
   ranks above the supply-disruption qualification because it is the primary
   path asserted for rising interest rates in the source.
6. **Resource bound:** the run finishes within 10 minutes and stays within the
   request limits above.

A missing required direction, an unsupported claim, absent provenance, a commit
without an approval, or a resource-limit violation is a failed demo—not a
warning to be waived after the run.
