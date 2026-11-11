# MemoryDM

MemoryDM manages conversational memory across raw messages, atomic facts, experience memory, and user profiles. It combines provenance tracking, hybrid search, agentic grep, and semantic forgetting.

## Quick start

Install Python 3.12 or later and [uv](https://docs.astral.sh/uv/), then run the following commands in the repository directory.

```bash
uv sync --locked
cp .env.example .env
```

Set `LLM_URL`, `LLM_API_KEY`, `LLM_MODEL`, and `LLM_PROVIDER` in `.env`. Set the embedding endpoint, API key, and model through `EMBEDDING_URL`, `EMBEDDING_API_KEY`, and `EMBEDDING_MODEL`. Use `openai_chat` for standard chat models, `openai` for reasoning models, or the provider listed in `.env.example` that matches your endpoint.

```bash
uv run memory-dm
```

Chat normally to build and search memory. Ask the assistant to forget information in natural language. Use `/sessions` to list conversations and `/exit` to save and quit. Memory is stored in `data/memory_dm.sqlite3`.

```bash
uv run memory-dm --user alice
uv run memory-dm --session <session-id>
```

## Evaluation

The datasets used in the paper are included in `datasets/bemula-lite/` and `datasets/locomo/`. Install the evaluation dependencies before running the benchmarks.

```bash
uv sync --locked --extra eval
uv run memory-dm-bemula --out data/bemula/run
uv run memory-dm-locomo --dataset datasets/locomo/locomo10.json --categories 1 2 3 4 --out data/locomo/run
```

The Bemula evaluator and the LoCoMo judge prompt are bundled with the code. Both runners accept `--resume` with the original output directory.

Score LoCoMo answers with the bundled LightMem accuracy prompt. The judge uses the model configured in the supplied environment file.

```bash
uv run python scripts/judge_locomo.py \
  --input data/locomo/run/items.jsonl --out data/locomo/run/judge \
  --env-file .env
```

Run the retrieval ablations with shared ingestion. Set `FORGET_MAX_PASSES=1` for single-pass forgetting, or change `MEMORY_DM_WRITE_EVERY` to evaluate write batch sizes.

```bash
uv run python scripts/run_locomo_retrieval_ablation.py \
  --dataset datasets/locomo/locomo10.json --out data/locomo/ablation
```

The storage audit scripts accept `--run`, `--dataset`, and `--out`. Use `--help` on any command to see its options.
