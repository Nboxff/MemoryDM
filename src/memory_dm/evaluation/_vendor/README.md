# Evaluation sources

`bemula/` contains the schemas, adapter interface, runner, judge, and metrics from the Bemula evaluator used in the paper. Imports point to the bundled package. The LLM interface, JSON parser, and gold answer constants are extracted from their source modules. Dataset generation and baseline implementations are omitted.

`memory_dm/prompts/locomo_judge.md` contains LightMem's `ACCURACY_PROMPT` from `experiments/locomo/llm_judge.py`, preserved verbatim. The accompanying MIT license is in `LICENSE.LightMem`.

`SOURCES.json` records the SHA-256 hashes of the source files and the extracted prompt.
