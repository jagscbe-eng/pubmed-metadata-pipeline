# Scientific Literature Metadata Extraction Pipeline

A Nextflow + Claude pipeline that turns scientific abstracts — supplied directly or
pulled live from PubMed by topic — into validated, structured JSON metadata for
downstream literature review workflows.

## Architecture

```
--topic "search terms"                      (optional: live PubMed search)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  fetch_pubmed_abstracts()                                    │
│  NCBI ESearch → PMIDs → NCBI EFetch (XML) → *.txt            │
│  written into abstracts_input/                                │
└───────────────────────────────────────────────────────────┘
        │
        ▼
abstracts_input/*.txt                       (raw scientific abstracts)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  main.nf  (Nextflow DSL2 orchestrator)                     │
│                                                             │
│  1. Channel.fromPath() stages the abstracts + extractor    │
│     script into an isolated task work directory            │
│  2. nextflow.config injects ANTHROPIC_API_KEY into that    │
│     task's environment                                      │
│  3. Runs: python pubmed_metadata_extractor.py               │
│            --input_dir .  --output_dir .  --domain ...      │
│  4. publishDir copies the resulting *.json back out         │
└───────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  pubmed_metadata_extractor.py                               │
│                                                             │
│  - argparse CLI: --input_dir / --output_dir / --domain /    │
│    --topic / --max_results                                  │
│  - Two selectable Pydantic schemas (DOMAIN_SCHEMAS):         │
│      • biomedical: pathogen, genes, antigen targets          │
│      • general: field of study, key concepts                │
│  - Calls Claude (claude-sonnet-5) with forced tool_choice    │
│    so the model must return exactly that schema's shape      │
│  - Validates every response against the Pydantic model       │
│    before writing it to disk                                 │
│  - Skips (rather than crashes on) two failure modes:          │
│      • ValidationError  – malformed/incomplete model output  │
│      • ExtractionRefused – Claude's safety classifier stops  │
│        generation (stop_reason == "refusal") on sensitive    │
│        dual-use content                                       │
└───────────────────────────────────────────────────────────┘
        │
        ▼
metadata_output/*_metadata.json             (validated structured metadata)
```

## Why Nextflow

Nextflow provides the orchestration layer around the Python extraction logic:

- **Staging** – each run gets an isolated work directory containing only the
  abstracts and script it needs, rather than the script reaching into a shared
  folder directly.
- **Declarative outputs** – `path "*.json"` + `publishDir` makes "what this run
  produced" explicit and reproducible, instead of relying on side-effecting writes.
- **Environment isolation** – secrets (the Anthropic API key) are injected per-task
  via `nextflow.config`'s `env` scope rather than baked into the script.

## Design decisions worth calling out

- **Structured extraction via forced tool use**: each domain's Pydantic model
  exposes `model_json_schema()` directly as the Claude tool's `input_schema`,
  with `tool_choice` forcing that specific tool — the model has no path to
  respond with free text.
- **Validate, don't trust**: every tool response is re-parsed through the
  matching Pydantic model before being written to disk, so a malformed or
  incomplete model response never silently produces bad output.
- **Fail soft per-file**: the batch loop catches failures per abstract and
  continues to the next file, so one bad/refused abstract doesn't take down
  an entire batch run.
- **Pluggable domain schemas**: `--domain` swaps which Pydantic model (and
  system prompt) is used, so the same pipeline can target biomedical literature
  today and a different field's schema tomorrow without touching the
  orchestration logic.
- **Retrieval is a separate concern from extraction**: `--topic` just populates
  `abstracts_input/` before the same extraction loop runs — swapping in a
  different literature source later (arXiv, Semantic Scholar) means adding a
  new fetch function, not rewriting the pipeline.
- **CLI-first design**: the extractor script works identically whether invoked
  directly (defaults to local OneDrive folders) or staged by Nextflow (explicit
  `--input_dir`/`--output_dir` pointing at the task work directory) — no
  pipeline-specific branching inside the script itself.

## Known limitation

`--topic` retrieval only queries PubMed, so results stay biomedical-flavored
regardless of which `--domain` schema is selected. Genuinely general-science
topics (physics, economics, etc.) would need a different literature source
wired in as a separate fetch function.

## Running it

**Standalone**, with live PubMed retrieval by topic:

```bash
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python pubmed_metadata_extractor.py --topic "CRISPR gene editing cancer therapy" --max_results 5 --domain biomedical
```

**Standalone**, over abstracts already sitting in `abstracts_input/`:

```bash
python pubmed_metadata_extractor.py --domain general
```

**Via Nextflow** (requires WSL2 on Windows — Nextflow does not run natively there):

```bash
nextflow run main.nf
```

## Files

| File | Purpose |
|---|---|
| `main.nf` | Nextflow DSL2 workflow: stages inputs, runs the extractor, publishes outputs |
| `nextflow.config` | Propagates `ANTHROPIC_API_KEY` into each task's environment |
| `pubmed_metadata_extractor.py` | PubMed retrieval, Claude-powered extraction, Pydantic validation |
