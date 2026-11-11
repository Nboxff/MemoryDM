"""Replace a user's compact profile at a session boundary, without source bookkeeping."""

import json
import sqlite3
from importlib.resources import files

from memory_dm.llm.clients import ClientError, LLMClient
from memory_dm.llm.structured import (
    ResponseValidationError,
    StructuredResponseError,
    request_structured,
)
from memory_dm.models import Turn
from memory_dm.profile.models import ProfileUpdate, UserProfile
from memory_dm.profile.tokens import PROFILE_TOKEN_BUDGET, PROFILE_TOKEN_LIMIT, token_count
from memory_dm.storage.profile import ProfileStore


class ProfileService:
    def __init__(self, store, client: LLMClient):
        self.repository, self.client = ProfileStore(store), client

    def maintain(self, user_id: str, *, session_id: str | None = None, force=False) -> dict:
        if session_id is not None:
            self.repository.store._require_session(session_id, user_id)
        before = self.repository.snapshot(user_id)
        progress = self.repository.connection.execute(
            "SELECT * FROM profile_progress WHERE user_id=?", (user_id,)
        ).fetchone()
        if not force and progress and progress["input_hash"] == before.signature:
            if progress["status"] == "completed":
                if self.repository.publish(user_id):
                    return {"status": "unchanged", "written": 0}
                return {
                    "status": "failed",
                    "written": 0,
                    "error_code": "PROFILE_FILE_SYNC_ERROR",
                    "committed": True,
                }
            if progress["attempts"] >= 3:
                return {
                    "status": "retry_exhausted",
                    "written": 0,
                    "error_code": "PROFILE_RETRY_EXHAUSTED",
                }
        if not before.memories and before.document is None:
            return {"status": "unchanged", "written": 0}
        return self._update(user_id, before, self._payload(before, session_id))

    def _payload(self, before, session_id):
        active = [m for m in before.memories.values() if m.status == "ACTIVE"]
        session_changes = (
            {
                r[0]
                for r in self.repository.connection.execute(
                    "SELECT memory_id FROM experience_session_changes WHERE session_id=?",
                    (session_id,),
                )
            }
            if session_id is not None
            else set()
        )

        def current_session(memory):
            return memory.id in session_changes or any(
                e.session_id == session_id for e in memory.evidence
            )

        # Read the complete old document. New session facts have priority over background.
        active.sort(
            key=lambda m: (
                not current_session(m),
                -m.created_at.timestamp(),
                m.id,
            )
        )
        facts = []
        for memory in active:
            item = {
                "type": memory.type,
                "content": memory.content,
                "valid_time": memory.valid_time.model_dump() if memory.valid_time else None,
                "current_session": current_session(memory),
            }
            facts.append(item)
        experiences = []
        for item in sorted(before.experiences.values(), key=lambda e: e.id):
            value = {"kind": item.kind, "content": item.content}
            experiences.append(value)
        old = before.document
        return {
            "current_profile": old.content if old else None,
            "atomic_facts": facts,
            "experiences": experiences,
            "token_budget": PROFILE_TOKEN_BUDGET,
            "tokenizer": "cl100k_base",
        }

    def _update(self, user_id, before, payload):
        old = before.document
        prompt = (
            files("memory_dm.prompts").joinpath("profile_extract.md").read_text(encoding="utf-8")
        )

        oversized = []

        def validate(result):
            if result.markdown is not None and token_count(result.markdown) > PROFILE_TOKEN_LIMIT:
                oversized.append(result.markdown)
                raise ResponseValidationError(
                    f"The whole Markdown has {token_count(result.markdown)} tokens, "
                    "exceeding the 1200-token limit; "
                    "compress it to at most 1000 cl100k_base tokens. Preserve important facts.",
                    issues=[
                        {
                            "code": "profile_token_limit",
                            "tokens": token_count(result.markdown),
                            "limit": PROFILE_TOKEN_LIMIT,
                        }
                    ],
                )

        try:
            try:
                proposed = request_structured(
                    self.client,
                    ProfileUpdate,
                    [
                        Turn(session_id="profile", role="system", content=prompt),
                        Turn(
                            session_id="profile",
                            role="user",
                            content=json.dumps(payload, ensure_ascii=False),
                        ),
                    ],
                    name="profile_update",
                    validate=validate,
                    temperature=0,
                )
            except StructuredResponseError as error:
                if not oversized or any(
                    issue["code"] != "profile_token_limit"
                    for issue in error.diagnostics.get("issues", [])
                ):
                    raise
                # A dedicated small compression input avoids regenerating the same
                # oversized document from the complete collection of facts.
                proposed = self._compress(oversized[-1], validate)
            document = (
                UserProfile(
                    user_id=user_id,
                    content=proposed.markdown,
                    **({"id": old.id, "created_at": old.created_at} if old else {}),
                )
                if proposed.markdown is not None
                else None
            )
            changed = document != before.document
            published = self.repository.commit(user_id, before, document)
            return {
                "status": "completed" if published else "failed",
                "written": int(changed),
                "tokens": token_count(proposed.markdown or ""),
                **(
                    {"error_code": "PROFILE_FILE_SYNC_ERROR", "committed": True}
                    if not published
                    else {}
                ),
            }
        except (ClientError, StructuredResponseError, ValueError, sqlite3.Error, OSError) as error:
            return self._failed(user_id, before, error)

    def _compress(self, markdown, validate):
        def check(result):
            if result.markdown is None:
                raise ResponseValidationError(
                    "Compression must preserve a useful profile, not erase it",
                    issues=[{"code": "profile_compression_empty"}],
                )
            validate(result)

        prompt = (
            files("memory_dm.prompts").joinpath("profile_compress.md").read_text(encoding="utf-8")
        )
        return request_structured(
            self.client,
            ProfileUpdate,
            [
                Turn(session_id="profile", role="system", content=prompt),
                Turn(
                    session_id="profile",
                    role="user",
                    content=json.dumps(
                        {
                            "markdown": markdown,
                            "measured_tokens": token_count(markdown),
                            "target_tokens": 700,
                            "tokenizer": "cl100k_base",
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            name="profile_compression",
            validate=check,
            temperature=0,
        )

    def _failed(self, user_id, before, error):
        with self.repository.connection:
            self.repository.connection.execute("BEGIN IMMEDIATE")
            if self.repository.snapshot_in_transaction(user_id).signature == before.signature:
                self.repository._progress(user_id, before.signature, "failed")
        return {
            "status": "failed",
            "written": 0,
            "error_code": type(error).__name__,
            "diagnostics": getattr(error, "diagnostics", {}),
        }
