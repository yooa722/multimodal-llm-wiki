# Architecture and comparison contract

## Layer contract

| Layer | Text Wiki baseline | Multimodal increment |
|---|---|---|
| Input | Same `mmwiki-0.1` Source Package | Same immutable source version |
| Evidence representation | Text, OCR, caption, linearized proxy | Complete rows/cells/HTML, LaTeX, original images |
| Wiki build | Text model organizes pages and WikiLinks | Vision model updates only affected knowledge pages |
| Index | Page BM25 + Page Embedding; separate Evidence text embedding and rerank | Reuse page/text vectors; add visual embedding and visual rerank |
| Query | Persistent Wiki page → source scope → text Evidence | Persistent Wiki page → original multimodal Evidence → VLM |
| Provenance | `source_id + source_version + item_id` | The same identity; no replacement by captions |

The original Karpathy LLM Wiki is an architectural pattern rather than a fixed executable benchmark. In this project, the reproducible baseline is therefore defined as the same packages, text-only representations, Wiki builder, test set, and text retrieval models. The treatment changes only by enabling first-class multimodal Evidence and visual retrieval.

Each stable page records `representation_layers`, `last_ingest_stage`, `revision`, source versions and Evidence IDs. `wiki/maintenance.md` reports stale versions, orphan pages, broken links and review candidates. Query never writes stable pages.

The Wiki page index is independently refreshable with `python3 app.py build-wiki-index`. This is the preferred architecture-migration path because it keys reuse by page content hash and leaves all Evidence text/visual vectors untouched.

## Required comparison dimensions

Capture both absolute values and the multimodal increment:

- build elapsed time, API calls, and token usage;
- index elapsed time and text-vector reuse rate;
- Wiki page, item, chunk, asset, and storage counts;
- Recall@5, MRR, Top-1, nDCG@5, Wiki source/page recall, fallback rate, and latency;
- modality-level results for text, table, image/chart, and equation questions;
- idempotent repeat cost for an unchanged source.

Do not claim that all percentages should reach 100%. Explain the sample count, denominator, retrieval scope, and whether any mode fell back.

## Demonstration sequence

1. Run preflight and show OpenCode/Skill/model/index readiness.
2. Open a Source Package and show its text proxy plus original table/image Evidence.
3. Show `--stage text` and `--stage multimodal` as separate operations.
4. Show vector reuse counters from incremental indexing.
5. Ask one table question, one genuinely visual question, and one insufficient-evidence question.
6. Open the cited Wiki page and original Evidence to prove traceability.
