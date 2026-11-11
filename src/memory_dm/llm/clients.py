import math
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from json import JSONDecodeError
from threading import BoundedSemaphore

from httpx import HTTPError
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from memory_dm.config import APIConfig
from memory_dm.llm.providers import get_adapter
from memory_dm.llm.usage import APIUsage, UsageCounter
from memory_dm.models import Turn


class ClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _translate_error(error: APIError) -> ClientError:
    if isinstance(error, APITimeoutError):
        return ClientError("API request timed out")
    if isinstance(error, APIConnectionError):
        return ClientError("Could not connect to the API")
    if isinstance(error, APIStatusError):
        return ClientError(f"API returned HTTP {error.status_code}", error.status_code)
    return ClientError("Invalid API response")


class _APIClient:
    def __init__(self, config: APIConfig):
        if not config.api_key.strip() or not config.model.strip():
            raise ValueError("API key and model must be configured before using this client")
        self.config = config
        self._usage = UsageCounter()
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    def close(self) -> None:
        self._client.close()

    def usage(self) -> APIUsage:
        return self._usage.snapshot()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class LLMClient(_APIClient):
    def __init__(self, config: APIConfig):
        self._adapter = get_adapter(config.provider)
        # Validate protocol/configuration conflicts before allocating a transport.
        self._adapter.adapt(config, {"response_format": {"type": config.json_mode}})
        super().__init__(config)

    def chat_json(
        self,
        turns: list[Turn],
        *,
        schema: dict,
        name: str,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        # Select by API capability, not vendor; callers validate the schema in both modes.
        response_format = {"type": self.config.json_mode}
        if self.config.json_mode == "json_schema":
            response_format["json_schema"] = {"name": name, "strict": True, "schema": schema}
        options = {"model": model} if model is not None else {}
        return self.chat(turns, response_format=response_format, temperature=temperature, **options)

    def chat(
        self,
        turns: list[Turn],
        *,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> str:
        if not turns:
            raise ValueError("At least one turn is required")
        if on_text is not None and response_format is not None:
            raise ValueError("Structured replies must be collected before validation")
        messages = [{"role": turn.role, "content": turn.content} for turn in turns]
        options = {"response_format": response_format} if response_format is not None else {}
        selected_model = model or self.config.model
        if max_tokens is not None and max_output_tokens is not None:
            raise ValueError("Use only one of max_tokens and max_output_tokens")
        # Per-call values win over configured defaults, including zero-valued sampling knobs.
        supplied = {
            "temperature": temperature,
            "top_p": top_p,
            "seed": seed,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "max_output_tokens": max_output_tokens if max_output_tokens is not None else max_tokens,
        }
        effective = {
            field: value if value is not None else getattr(self.config, field)
            for field, value in supplied.items()
        }
        # Reuse the same range/type checks for per-call and environment configuration.
        replace(self.config, **effective)
        options.update({field: value for field, value in effective.items() if value is not None})
        options = self._adapter.adapt(self.config, options)
        if response_format is not None:
            instruction = "Return only valid JSON matching the requested schema."
            if messages[0]["role"] == "system":
                messages[0] = {
                    **messages[0],
                    "content": messages[0]["content"] + "\n\n" + instruction,
                }
            else:
                messages.insert(0, {"role": "system", "content": instruction})
        self._usage.started()
        try:
            if on_text is not None:
                return self._stream_chat(selected_model, messages, options, on_text)
            response = self._client.chat.completions.create(
                model=selected_model, messages=messages, **options
            )
        except JSONDecodeError as error:
            raise ClientError("API returned invalid JSON") from error
        except APIError as error:
            raise _translate_error(error) from error
        usage = getattr(response, "usage", None)
        self._usage.received(
            getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
        )
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise ClientError("API returned no text reply")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason in ("length", "content_filter"):
            raise ClientError("API reply was incomplete or filtered")
        if response_format is not None and finish_reason != "stop":
            raise ClientError("API returned an incomplete structured reply")
        message = getattr(choice, "message", None)
        if getattr(message, "refusal", None):
            raise ClientError("API refused the request")
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ClientError("API returned no text reply")
        return content

    def _stream_chat(self, model, messages, options, on_text) -> str:
        """Emit content deltas, but return successfully only after a complete text reply."""
        parts = []
        finished = False
        usage = None
        try:
            with self._client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                **options,
            ) as stream:
                for chunk in stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = chunk_usage
                    choices = getattr(chunk, "choices", None)
                    if not isinstance(choices, list):
                        raise ClientError("API returned an invalid stream")
                    if not choices:
                        continue  # Usage-only trailer.
                    choice = choices[0]
                    if getattr(choice, "index", None) != 0:
                        raise ClientError("API returned an unexpected stream choice")
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        raise ClientError("API returned an invalid stream delta")
                    if getattr(delta, "refusal", None):
                        raise ClientError("API refused the request")
                    reason = getattr(choice, "finish_reason", None)
                    if reason not in (None, "stop"):
                        raise ClientError("API reply was incomplete or filtered")
                    content = getattr(delta, "content", None)
                    if content is not None and not isinstance(content, str):
                        raise ClientError("API returned an invalid stream delta")
                    if content:
                        if finished:
                            raise ClientError("API sent content after the reply ended")
                        parts.append(content)
                        on_text(content)
                    if reason == "stop":
                        finished = True
        except HTTPError as error:
            raise ClientError("Connection lost while receiving the reply") from error
        finally:
            self._usage.received(
                getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
            )
        if not finished:
            raise ClientError("API reply stream ended before completion")
        content = "".join(parts)
        if not content.strip():
            raise ClientError("API returned no text reply")
        return content


class LLMModelView:
    """Select another model on the same endpoint, sharing transport and usage counters."""

    def __init__(self, client: LLMClient, model: str):
        if not model.strip():
            raise ValueError("A model name is required")
        self.client, self.config = client, replace(client.config, model=model)

    def chat_json(self, turns, **kwargs):
        return self.client.chat_json(turns, model=self.config.model, **kwargs)

    def usage(self):
        return self.client.usage()


class EmbeddingClient(_APIClient):
    def __init__(self, config: APIConfig):
        super().__init__(config)
        # One shared budget also bounds overlapping embed()/embed_batch() calls on this client.
        # Workers perform HTTP only; SQLite transactions stay on the caller's thread.
        self._request_slots = BoundedSemaphore(config.max_concurrency)

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("Embedding input must not be blank")
        return self._embed_inputs(text)[0]

    def embed_batch(self, texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        batch_size = self.config.batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("Embedding batch size must be a positive integer")
        if not isinstance(texts, list) or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise ValueError("Embedding inputs must be non-blank strings")
        if not texts:
            return []
        starts = iter(range(0, len(texts), batch_size))
        batches = {}
        if len(texts) <= batch_size or self.config.max_concurrency == 1:
            for start in starts:
                batches[start] = self._embed_inputs(texts[start : start + batch_size])
        else:
            executor = ThreadPoolExecutor(max_workers=self.config.max_concurrency)
            pending = {}

            def submit_next() -> None:
                start = next(starts, None)
                if start is not None:
                    pending[
                        executor.submit(self._embed_inputs, texts[start : start + batch_size])
                    ] = start

            try:
                # Bound queued work as well as active requests for large imports.
                for _ in range(self.config.max_concurrency):
                    submit_next()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        batches[pending.pop(future)] = future.result()
                    for _ in done:
                        submit_next()
            finally:
                # Cancel unsent work after failure, and finish active calls before closing HTTP.
                executor.shutdown(wait=True, cancel_futures=True)
        vectors = [vector for start in sorted(batches) for vector in batches[start]]
        if len({len(vector) for vector in vectors}) != 1:
            raise ClientError("API returned inconsistent embedding dimensions")
        return vectors

    def _embed_inputs(self, inputs: str | list[str]) -> list[list[float]]:
        try:
            with self._request_slots:
                self._usage.started()
                response = self._client.embeddings.with_raw_response.create(
                    model=self.config.model, input=inputs, encoding_format="float"
                )
                # Validate raw values before SDK coercion can turn booleans into numeric vectors.
                payload = response.http_response.json()
        except JSONDecodeError as error:
            raise ClientError("API returned invalid JSON") from error
        except APIError as error:
            raise _translate_error(error) from error
        usage = payload.get("usage") if isinstance(payload, dict) else None
        self._usage.received(usage.get("prompt_tokens") if isinstance(usage, dict) else None)
        data = payload.get("data") if isinstance(payload, dict) else None
        count = 1 if isinstance(inputs, str) else len(inputs)
        if not isinstance(data, list) or len(data) != count:
            raise ClientError("API returned an unexpected number or index of embeddings")
        ordered = {}
        for item in data:
            index = item.get("index") if isinstance(item, dict) else None
            if type(index) is not int or index not in range(count) or index in ordered:
                raise ClientError("API returned an unexpected number or index of embeddings")
            vector = item.get("embedding")
            try:
                valid = (
                    isinstance(vector, list)
                    and bool(vector)
                    and all(
                        type(value) in (int, float) and math.isfinite(value) for value in vector
                    )
                )
            except OverflowError:
                valid = False
            if not valid:
                raise ClientError("API returned an invalid embedding")
            ordered[index] = vector
        vectors = [ordered[index] for index in range(count)]
        if len({len(vector) for vector in vectors}) != 1:
            raise ClientError("API returned inconsistent embedding dimensions")
        return vectors
