#!/usr/bin/env python3
"""
Manual integration test for BedrockKBRetriever against live AWS resources.

Not part of the offline pytest suite. Run this after provisioning a Bedrock Knowledge
Base backed by an S3 data source containing the supplier corpus.

REQUIRED ENV VARS
    BEDROCK_KB_ID         Bedrock Knowledge Base ID (e.g. ABCD1234EF)
    CORPUS_S3_BUCKET      S3 bucket that is the KB data source

OPTIONAL ENV VARS
    AWS_DEFAULT_REGION    Default: us-west-2
    CORPUS_S3_PREFIX      Key prefix in the bucket. Default: supplier-profiles/
    BEDROCK_KB_TOP_K      Results to request per query. Default: 4

AWS CREDENTIALS
    Standard boto3 credential chain (IAM role, env vars, ~/.aws/credentials).
    The executing principal needs:
        s3:PutObject    on s3://{CORPUS_S3_BUCKET}/{CORPUS_S3_PREFIX}*
        bedrock:Retrieve on arn:aws:bedrock:{region}:{account}:knowledge-base/{BEDROCK_KB_ID}

USAGE
    # Full run (publish + prompt for KB sync + assertions)
    BEDROCK_KB_ID=ABCD1234EF CORPUS_S3_BUCKET=my-kb-bucket \\
        python scripts/integration_bedrock_kb.py

    # Skip publish/sync — re-run assertions only (corpus already in S3, KB already synced)
    BEDROCK_KB_ID=ABCD1234EF CORPUS_S3_BUCKET=my-kb-bucket \\
        python scripts/integration_bedrock_kb.py --skip-publish

EXPECTED OUTPUT (all assertions passing)
    Phase 1 — Publishing supplier corpus to S3
      uploaded s3://my-kb-bucket/supplier-profiles/TSMC.txt
      uploaded s3://my-kb-bucket/supplier-profiles/TSMC.txt.metadata.json
      ... (6 objects total)

    Phase 2 — Sync the Knowledge Base (manual step)
      ... instructions ...
    Press Enter when sync is COMPLETE →

    Phase 3 — Running retrieval assertions
      Retriever: bedrock_kb_supplier_profiles v1 | top_k=4 | embedding=bedrock_managed

      PASS  TSMC — filtered retrieval by supplier metadata
      PASS  Murata — filtered retrieval by supplier metadata
      PASS  Foxconn — filtered retrieval by supplier metadata
      PASS  unfiltered query — non-empty context returned

    Results: 4 passed, 0 failed
"""

import os
import sys
from pathlib import Path

# ---- project root on path ----
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ---- env var checks ----
_REQUIRED = {"BEDROCK_KB_ID": "Bedrock Knowledge Base ID", "CORPUS_S3_BUCKET": "S3 bucket for KB data source"}
_MISSING = [k for k in _REQUIRED if not os.environ.get(k)]
if _MISSING:
    print("error: missing required environment variables:", file=sys.stderr)
    for k in _MISSING:
        print(f"  {k:<25} {_REQUIRED[k]}", file=sys.stderr)
    sys.exit(1)

KB_ID = os.environ["BEDROCK_KB_ID"]
BUCKET = os.environ["CORPUS_S3_BUCKET"]
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
PREFIX = os.environ.get("CORPUS_S3_PREFIX", "supplier-profiles/")

_PROFILES_PATH = _PROJECT_ROOT / "app" / "storage" / "supplier_profiles.json"

# Force bedrock_kb provider so the script does not depend on the caller setting it.
os.environ["RETRIEVER_PROVIDER"] = "bedrock_kb"

from app.ingestion.publish_supplier_corpus import publish
from app.retrieval.retriever import get_retriever


# Phrases that must appear in retrieval results, derived from supplier_profiles.json.
_TEST_CASES = [
    {
        "desc": "TSMC — filtered retrieval by supplier metadata",
        "query": "Taiwan semiconductor foundry earthquake drought water shortage export controls",
        "suppliers": ["TSMC"],
        # Phrases lifted verbatim from the TSMC profile entry.
        "expect": ["TSMC", "semiconductor foundry", "Taiwan"],
    },
    {
        "desc": "Murata — filtered retrieval by supplier metadata",
        "query": "passive electronic components Japan factory natural disaster capacity constraints",
        "suppliers": ["Murata"],
        "expect": ["Murata", "passive electronic components"],
    },
    {
        "desc": "Foxconn — filtered retrieval by supplier metadata",
        "query": "electronics manufacturing China Vietnam labor unrest geopolitical logistics",
        "suppliers": ["Foxconn"],
        "expect": ["Foxconn", "electronics manufacturing"],
    },
    {
        "desc": "unfiltered query — non-empty context returned",
        "query": "supply chain disruption manufacturing risk",
        "suppliers": [],
        "expect": [],  # only verified non-empty (no filter, KB picks best match)
    },
]


def _phase1_publish() -> None:
    print("=" * 60)
    print("Phase 1 — Publishing supplier corpus to S3")
    print("=" * 60)
    try:
        keys = publish(_PROFILES_PATH, BUCKET, PREFIX, REGION)
        print(f"\n{len(keys)} objects uploaded to s3://{BUCKET}/{PREFIX}\n")
    except Exception as exc:
        print(f"\nerror: S3 publish failed — {exc}", file=sys.stderr)
        print(
            "  Verify that CORPUS_S3_BUCKET exists in the correct region and that\n"
            "  AWS credentials are configured with s3:PutObject access.",
            file=sys.stderr,
        )
        sys.exit(1)


def _phase2_sync_prompt() -> None:
    print("=" * 60)
    print("Phase 2 — Sync the Knowledge Base (manual step)")
    print("=" * 60)
    print()
    print("Trigger KB re-indexing to pick up the uploaded corpus:")
    print()
    print("  Option A — AWS CLI:")
    print(f"    aws bedrock-agent start-ingestion-job \\")
    print(f"        --knowledge-base-id {KB_ID} \\")
    print(f"        --data-source-id $BEDROCK_KB_DATA_SOURCE_ID")
    print()
    print("  Option B — AWS Console:")
    print("    Knowledge Bases → your KB → Data source → Sync")
    print()
    print("Wait for the sync job to reach COMPLETE status. Typical duration: 1–3 minutes")
    print("for 3 supplier profiles.")
    print()
    input("Press Enter when sync is COMPLETE → ")
    print()


def _phase3_assertions() -> None:
    print("=" * 60)
    print("Phase 3 — Running retrieval assertions")
    print("=" * 60)

    retriever = get_retriever(_PROFILES_PATH)
    print(
        f"\nRetriever: {retriever.retriever_id} {retriever.retriever_version}"
        f" | top_k={retriever.top_k} | embedding={retriever.embedding_provider}\n"
    )

    passed = 0
    failed = 0
    _outcomes: list[str | None] = []  # context per test case; None if the API call errored

    for tc in _TEST_CASES:
        try:
            result = retriever.retrieve(tc["query"], tc["suppliers"])
            context = result.context
            _outcomes.append(context)
        except Exception as exc:
            print(f"  ERROR {tc['desc']}")
            print(f"        API call failed: {exc}")
            _outcomes.append(None)
            failed += 1
            continue

        missing_phrases = [p for p in tc["expect"] if p not in context]
        empty_fail = not tc["expect"] and context == "No context found"

        if missing_phrases or empty_fail:
            print(f"  FAIL  {tc['desc']}")
            if missing_phrases:
                print(f"        Phrases not found in context: {missing_phrases}")
            if empty_fail:
                print("        Expected non-empty context; got 'No context found'")
            print(f"        Context (first 300 chars): {context[:300]!r}")
            failed += 1
        else:
            print(f"  PASS  {tc['desc']}")
            passed += 1

    print()
    print(f"Results: {passed} passed, {failed} failed")

    if failed:
        # Pattern analysis: identify which failure mode is most likely and print a
        # targeted hint before the generic checklist.
        _non_error = [o for o in _outcomes if o is not None]
        _filtered_outcomes = [
            _outcomes[i] for i, tc in enumerate(_TEST_CASES) if tc["suppliers"]
        ]
        _unfiltered_outcomes = [
            _outcomes[i] for i, tc in enumerate(_TEST_CASES) if not tc["suppliers"]
        ]
        _all_no_context = bool(_non_error) and all(o == "No context found" for o in _non_error)
        _unfiltered_has_results = any(
            o is not None and o != "No context found" for o in _unfiltered_outcomes
        )
        _filtered_all_empty = bool(
            any(o is not None for o in _filtered_outcomes)
            and all(o == "No context found" for o in _filtered_outcomes if o is not None)
        )

        if _all_no_context:
            print()
            print("  Diagnosis: all queries returned 'No context found'.")
            print("  The most likely cause is that the KB sync has not completed or the index")
            print("  hasn't propagated yet. Wait 2–3 minutes after sync reaches COMPLETE,")
            print("  then re-run with --skip-publish.")
        elif _filtered_all_empty and _unfiltered_has_results:
            print()
            print("  Diagnosis: the unfiltered query returned results but all supplier-filtered")
            print("  queries returned nothing. This strongly indicates the .metadata.json sidecar")
            print("  files are absent or have the wrong attribute key ('supplier'). Re-run the")
            print("  full script without --skip-publish to re-upload the corpus with sidecars.")

        print()
        print("Troubleshooting:")
        print("  1. Confirm the KB sync job reached COMPLETE status in the console.")
        print("  2. Confirm BEDROCK_KB_ID matches the KB using the S3 data source bucket.")
        print("  3. Verify sidecar .metadata.json files are present in S3 — required for")
        print("     supplier-filtered queries. Re-run without --skip-publish if unsure.")
        print("  4. Allow 1–2 minutes after COMPLETE — index propagation can lag.")
        print("  5. Confirm the executing role has bedrock:Retrieve on this KB ARN.")
        print("  6. Confirm AWS credentials are configured (IAM role, env vars, or ~/.aws/credentials).")
        sys.exit(1)

    print()
    print("All assertions passed. BedrockKBRetriever is working against live AWS resources.")


def main() -> None:
    skip_publish = "--skip-publish" in sys.argv

    if skip_publish:
        print("(--skip-publish: skipping corpus upload and KB sync prompt)\n")
        _phase3_assertions()
    else:
        _phase1_publish()
        _phase2_sync_prompt()
        _phase3_assertions()


if __name__ == "__main__":
    main()
