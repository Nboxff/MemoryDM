"""A fixed text budget, independent of the configured LLM vendor."""

import base64
import gzip
import hashlib
from functools import lru_cache
from importlib.resources import files

import tiktoken

PROFILE_TOKEN_BUDGET = 1000  # Prompt target.
PROFILE_TOKEN_LIMIT = 1200  # Hard validation limit.


@lru_cache(maxsize=1)
def encoding():
    # Bundle the public vocabulary so profile reads, migrations and tests never
    # download tokenizer data or depend on a machine's temporary cache.
    data = gzip.decompress(
        files("memory_dm.profile").joinpath("vocabulary/cl100k_base.tiktoken.gz").read_bytes()
    )
    if hashlib.sha256(data).hexdigest() != (
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ):
        raise ValueError("The bundled profile tokenizer vocabulary is corrupted")
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in data.splitlines())
    }
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=(
            r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"
            r" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
        ),
        mergeable_ranks=ranks,
        special_tokens={},
    )


def token_count(text: str) -> int:
    return len(encoding().encode(text, disallowed_special=()))


def shorten_legacy_profile(text: str) -> str:
    """Only migration clips old unbounded documents; new updates must pass validation."""
    if token_count(text) <= PROFILE_TOKEN_LIMIT:
        return text
    prefix = encoding().decode(
        encoding().encode(text, disallowed_special=())[:PROFILE_TOKEN_BUDGET], errors="ignore"
    )
    prefix = prefix.rsplit("\n", 1)[0] if "\n" in prefix else prefix
    while token_count(prefix) > PROFILE_TOKEN_BUDGET:
        prefix = prefix[:-1]
    return prefix.strip()
