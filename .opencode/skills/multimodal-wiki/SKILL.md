---
name: multimodal-wiki
description: Operate and explain this project's traceable multimodal LLM Wiki in OpenCode. Use for the Chinese beginner walkthrough, staged text-to-multimodal builds, Wiki navigation, cited text/table/image question answering, evidence inspection, baseline comparison, readiness checks, and live demonstrations based on mmwiki-0.1 Source Packages.
---

# Multimodal LLM Wiki

Work from the repository root. Treat OpenCode as the interaction layer, Wiki pages as the knowledge backbone, and raw Evidence as the factual source. Respond in Chinese unless the user asks otherwise.

Before architecture work, distinguish the layers: `wiki-purpose.md` defines why the Wiki exists, `schema.md` defines page rules, stable Markdown pages hold compiled knowledge, page-level BM25/embeddings locate knowledge, and Chunk/Item/Asset Evidence proves the answer. Never collapse these into one generic RAG step.

## Beginner-first rule

When the user is unfamiliar with OpenCode, do not begin with raw JSON, Python commands, or API concepts. Run:

```bash
python3 tools/opencode_demo.py start
```

Then explain only these project commands: `/wiki-start`, `/wiki-demo`, `/wiki-table`, `/wiki-image`, and `/wiki-ask <问题>`. State explicitly that OpenCode is the operation console, not the Wiki storage or a Wikipedia-like website.

## Choose the workflow

- Prefer the typed OpenCode tools `wiki_start`, `wiki_status`, `wiki_tour`, `wiki_compare`, and `wiki_query` when available. They pass user questions as process arguments without shell interpolation. Use the Python or shell commands below only as fallbacks.
- For a first visit, run `bash .opencode/skills/multimodal-wiki/scripts/demo.sh start`.
- For a human-readable readiness check, run `bash .opencode/skills/multimodal-wiki/scripts/demo.sh status`.
- For the full read-only walkthrough, run `bash .opencode/skills/multimodal-wiki/scripts/demo.sh tour`.
- For a new Source Package, follow the staged build below. Do not use a one-shot build when collecting comparison metrics.
- For a question, use `auto` by default. The lightweight incremental Wiki path uses BM25 + MinerU Caption; it reaches Hybrid or Multimodal retrieval only when the corresponding feature switches are explicitly enabled.
- For benchmark or architecture explanations, read `references/architecture.md` first.

## Import an existing Markdown Wiki

Use the same `multimodal-wiki` Skill and its `wiki_import` operation for a user's existing local Wiki. The original directory is read-only; the command creates a derived view under `runtime/vault/wiki/external/<wiki_id>/`:

```bash
python3 app.py ingest-wiki \
  /absolute/path/to/wiki \
  --caption-package /absolute/path/to/mmwiki-package
```

The importer supports local relative Markdown images and Obsidian image links. It matches image bytes to MinerU assets by SHA-256, materializes MinerU Caption as derived Markdown `alt`, and leaves the original Markdown, WikiLinks, and image files untouched. Remote, absolute, and path-traversing images are rejected and reported without failing the whole Wiki.

The default configuration is deliberately lightweight:

```dotenv
MMWIKI_ENABLE_VLM=false
MMWIKI_ENABLE_VECTOR_RETRIEVAL=false
```

Use `python3 app.py wiki-status` for `wiki_status`. CLI flags `--vlm on|off` and `--vector-retrieval on|off` temporarily override `.env`. A visual query with these switches off safely falls back to BM25 + Caption and reports the reason; it never calls VLM, Embedding, or Rerank implicitly.

## Build in two stages

1. Validate the immutable parser handoff:

   ```bash
   python3 app.py validate /absolute/path/to/package
   ```

2. Build the text LLM Wiki baseline. It may consume OCR, captions, and linearized text proxies already supplied by the parser, but this stage must not call the new image OCR/VLM path, read image pixels, or use structured table cells:

   ```bash
   python3 app.py ingest /absolute/path/to/package --provider api --stage text
   python3 app.py build-index --text-only --vector-retrieval on
   ```

3. Add first-class table, equation, and image Evidence without rebuilding the immutable source or existing text vectors. `--vlm on` also builds image-derived OCR and semantic Caption Evidence:

   ```bash
   python3 app.py ingest /absolute/path/to/package \
     --provider api --stage multimodal --vlm on
   python3 app.py build-index --source-id <package-id> --vlm on --vector-retrieval on
   ```

   During a VLM-enabled multimodal stage, resolve a cost-aware policy before calling a model: natural images use VLM Caption first and persistent OCR only when parser metadata requests it; diagrams/charts and page screenshots use OCR + VLM Caption; table screenshots keep structured rows/cells/html as the primary representation and use OCR only as a check; formulas keep LaTeX and do not receive an ordinary Caption by default. The derived records and their `processing_policy` are stored in `state.sources[package_id].visual_evidence`, exposed as child Evidence for BM25/text embeddings, and mapped back to the parent Item, Chunk, Asset, and original image. Do not build duplicate visual vectors for these text records. Reuse the SHA-256 cache under `runtime/build-cache/visual/`.

4. Report the stage metrics returned by the commands: elapsed time, API calls, token usage, page impact scope, active item/chunk/asset counts, and reused/new Wiki-page, Evidence-text and visual index records.

If a valid Evidence index already exists and only the independent page-level index is missing or stale, run `python3 app.py build-wiki-index --vector-retrieval on`. This command must preserve all existing text and visual Evidence vectors and embed only changed Wiki pages.

Use `--full-scale` only on the multimodal stage when every page image must be analyzed. It persists the same OCR/Caption child Evidence as the normal multimodal path, but reuses page-level image annotations for Caption instead of issuing a duplicate vision-model call. The stage records a secret-free visual-build contract and signature, so enabling VLM after a text-only/API build triggers the missing visual representation; repeating the same source version and signature returns `unchanged` with zero model calls.

## Query with evidence

In OpenCode, call the typed `wiki_query` tool with `question`, `mode`, and `provider`. Never concatenate a user question into a shell command.

Use `auto` unless the user explicitly selects a mode. The backend owns the single visual-intent classifier: color/depth, left/right/up/down/position, arrows/connections, curves/bars/trends, objects/people/scenes, and visual quantity/spatial relations select Multimodal; ordinary text, number and structured-table facts select Hybrid. Hybrid answers must stay text-only. Only when the answer model declares the Hybrid evidence insufficient and a candidate Item has an original image may the pipeline upgrade once to Multimodal.

Every `/wiki-*` command body must contain only user-facing natural language. Do not place tool names, parameters, `mmwiki-action` comments or other routing metadata in command bodies because OpenCode may display the expanded command as the user message. Keep tool selection and safety rules in the presenter agents. For `/wiki-ask`, the dedicated `wiki-query-presenter` calls `wiki_query` once with the complete question, `mode=auto`, and `provider=api`; never expose these internal parameters to the user or execute the question through Bash.

The typed tool returns a titled result whose `output` is presentation-ready final Markdown. The presenter agent calls the tool, and the project-level `wiki-result-passthrough` plugin replaces the completed assistant text with that exact output. OpenCode is the primary answer surface: short citations such as `〔1〕` map directly to the matching `〔1〕` evidence card in the same assistant message, without a separate citation-index section. Each evidence card exposes source title, page, paragraph or typed region, section, original excerpt and full Evidence ID, followed by direct links to the stable Wiki page, the immutable source Evidence anchor and the original asset when available. These locations come from the deterministic `runtime/page-index.json`, derived from the current user's Source Package rather than hard-coded demo sources. Do not expose the localhost split-query workspace as the primary evidence action; keep it only for backward compatibility and internal debugging. Image cards render the original image inline before the derived descriptions, and separately label VLM understanding, Image OCR and MinerU Caption with their provenance. Do not summarize, rewrite, reorder, translate, or reconstruct this output. Never drop its Wiki HTTP links, Evidence IDs, evidence excerpts, complete tables, image links/previews, derived-description labels, or runtime table. This passthrough rule applies to every question type, not only demo cases.

Run directly through the CLI:

```bash
python3 app.py query "<question>" --retrieval-mode auto --top-k 5
python3 app.py query "<visual question>" --retrieval-mode multimodal --top-k 5
```

Or start the localhost API for an interactive OpenCode demo:

```bash
bash .opencode/skills/multimodal-wiki/scripts/demo.sh serve
```

Always show the answer, Evidence IDs, source version, requested/actual retrieval mode, routing or upgrade reason, model, latency, and whether fallback occurred. Never turn a query answer into a stable Wiki page automatically.

The query order is fixed: rank persistent Wiki pages first (`page_bm25` and, when built, `page_embedding`), derive the relevant source scope, then retrieve Chunk/Item Evidence. A high-scoring Wiki page is orientation, not proof; the final answer must still cite raw Evidence.

Use this fixed presentation order for every answer:

1. **结论** — answer or explicit insufficient-evidence refusal.
2. **知识入口** — at most two stable/source/evidence-map pages used for orientation; do not expose retrieval-channel names or long internal summaries.
3. **证据依据** — numbered cards that directly match inline `〔n〕` citations and include source, page, paragraph/typed region, section, original excerpt and full Evidence ID; keep complete table rows/cells and original image links/previews, and show the original image before separate VLM understanding, OCR and MinerU Caption representations.
4. **运行信息** — requested/actual retrieval mode, model, fallback, latency, and token usage.

Do not present a caption as if it were the original image, or linearized text as if it were the complete table. For visual questions, preserve the exact matched image instead of substituting another asset from the same item.

Answer depth must follow the user's task rather than a fixed length: keep a direct fact concise, but preserve ordered steps, comparison dimensions, requested list items, calculation conditions, and visible diagram relations when the question asks for them.

Preserve the localhost HTTP links returned by the typed tools. Do not replace them with absolute filesystem Markdown links: OpenCode Desktop may treat local paths as web URLs and fail to open them. For an image, show both the inline HTTP preview and the browser link returned by the tool. The tool may start the `127.0.0.1:19828` display service automatically; never bind it to a public interface.

## Verify before handoff

```bash
python3 -m unittest discover -s tests -v
python3 app.py lint
python3 tools/opencode_demo.py status
```

Do not print, copy, or commit `.env` or API keys. Do not mutate Source Package items, tables, images, or provenance. Keep `runtime/raw/<package-id>/<version>/` immutable and reject path traversal.
Read `runtime/vault/wiki/maintenance.md` after lint. Treat stale source versions, broken links and orphan pages as maintenance findings; do not silently rewrite pages during Query.
