"""Retrieved candidates, provenance repairs and bounded verification before one commit."""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from hashlib import sha256

from memory_dm.config import ForgettingOptions
from memory_dm.experience.forgetting import ExperienceForgetPlan, plan_experience_forgetting
from memory_dm.forgetting.batches import SemanticBatches, verification_items
from memory_dm.forgetting.cost import repair_cost
from memory_dm.forgetting.discovery import ForgetDiscovery
from memory_dm.forgetting.feedback import repair_feedback
from memory_dm.forgetting.models import (
    ForgetReport,
    ForgetSpec,
    ForgettingError,
)
from memory_dm.forgetting.policy import PolicyForgetter
from memory_dm.forgetting.probes import run_probes
from memory_dm.forgetting.replacement import prepare_replacements
from memory_dm.forgetting.scoped_storage import ScopedForgetRepository
from memory_dm.forgetting.storage import ForgetSnapshot
from memory_dm.llm.clients import EmbeddingClient, LLMClient
from memory_dm.models import Evidence, date_bounds, utc_now
from memory_dm.profile.forgetting import plan_profile_forgetting
from memory_dm.storage.sqlite import SQLiteStore


@dataclass
class BaseRepair:
    memories: dict
    turns: dict
    feedback: dict = field(default_factory=dict)
    orphaned: dict = field(default_factory=dict)
    verification_cache: dict = field(default_factory=dict)
    passes: int = 0
    pending: set = field(default_factory=set)
    safe_turns: dict = field(default_factory=dict)
    safe_memories: dict = field(default_factory=dict)


class ForgettingService:
    def __init__(
        self,
        store: SQLiteStore,
        llm: LLMClient,
        embedding: EmbeddingClient | None = None,
        *,
        batch_size: int = 16,
        batch_chars: int = 24000,
        max_passes: int = 2,
        context_turns: int = 12,
        probe_count: int = 0,
        probe_answer=None,
        options: ForgettingOptions | None = None,
    ):
        for value in (batch_size, batch_chars, max_passes, context_turns):
            if type(value) is not int or value <= 0:
                raise ValueError("Forgetting limits must be positive integers")
        self.store = store
        self._candidate_keys = None
        self.options = options or ForgettingOptions()
        self.repository = ScopedForgetRepository(store)
        self.semantic = PolicyForgetter(llm, self.options)
        self.embedding = embedding
        self.batch_size = batch_size
        self.batch_chars = batch_chars
        self.max_passes = max_passes
        self.context_turns = context_turns
        if type(probe_count) is not int or not 0 <= probe_count <= 5:
            raise ValueError("Runtime probe count must be between zero and five")
        if probe_count and probe_answer is None:
            raise ValueError("Runtime probes require a read-only answer callback")
        self.probe_count, self.probe_answer = probe_count, probe_answer

    def forget(
        self,
        request: str,
        user_id: str,
        *,
        reference_time: datetime | None = None,
        session_id: str | None = None,
    ) -> ForgetReport:
        if not user_id.strip():
            raise ValueError("A user ID is required")
        reference_time = reference_time or utc_now()
        history = []
        if session_id is not None:
            self.store._require_session(session_id, user_id)
            from memory_dm.models import Turn

            rows = self.store.connection.execute(
                "SELECT * FROM conversation_turns WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
                (session_id, self.context_turns),
            ).fetchall()
            history = [Turn.model_validate(dict(row)) for row in reversed(rows)]
        spec = self.semantic.parse(request, reference_time=reference_time, history=history)
        return self.execute(spec, user_id, reference_time=reference_time, session_id=session_id)

    def execute(
        self,
        spec: ForgetSpec,
        user_id: str,
        *,
        reference_time: datetime | None = None,
        session_id: str | None = None,
    ) -> ForgetReport:
        if not user_id.strip():
            raise ValueError("A user ID is required")
        if isinstance(self.repository, ScopedForgetRepository):
            self.repository.reset()
        self._candidate_keys = None
        before = self.repository.snapshot(user_id)
        replacement = prepare_replacements(
            spec,
            user_id,
            self.store,
            self.semantic,
            reference_time=reference_time or utc_now(),
            session_id=session_id,
        )
        runner = SemanticBatches()
        state, experience_plan = self._repair_retrieved(spec, user_id, before, runner)
        self.repository.profile_loaded = True
        from memory_dm.storage.profile import ProfileStore

        before.profiles = ProfileStore(self.store).read(user_id)
        if self.repository.signature(user_id) != before.signature:
            raise ForgettingError("User data changed during forgetting; retry the request")
        profile_plan = plan_profile_forgetting(
            spec,
            before,
            self.semantic,
            self._batches,
            self.max_passes,
            runner=runner,
        )
        state.memories.update((m.id, m) for m in replacement.memories)
        state.turns.update((t.turn_id, t) for t in replacement.turns)
        failures = (
            verification_items(state.feedback) + experience_plan.failures + profile_plan.failures
        )
        return self._commit(
            spec,
            user_id,
            before,
            state.memories,
            state.turns,
            replacement,
            experience_plan,
            profile_plan,
            failures,
            state.passes,
            state.feedback,
            runner.public(),
        )

    def _repair_retrieved(self, spec, user_id, before, runner):
        state = BaseRepair({}, {})
        discovery = ForgetDiscovery(self.store, self.embedding)
        self._candidate_keys = set()
        experience_plan = ExperienceForgetPlan({}, [], 0)
        previous_experience_ids = set()
        candidates = self._search_candidates(discovery, spec, user_id, runner)
        for _ in range(self.max_passes):
            state.passes += 1
            expanded = self.repository.expand(user_id, before, state, candidates)
            state.pending = expanded - self._candidate_keys
            self._candidate_keys.update(expanded)
            eligible = self._eligible(spec, before)
            for key, value in before.turns.items():
                state.safe_turns.setdefault(key, value)
            for key, value in before.memories.items():
                state.safe_memories.setdefault(key, value)
            previous_turns = dict(state.turns)
            application_feedback = self._repair_layer(
                "RAW", spec, before, state, eligible, previous_turns, runner
            )
            self._restore_orphans(state.orphaned, state.memories, state.turns)
            state.orphaned.update(
                self._repair_evidence(
                    state.memories, state.turns, {**before.turns, **previous_turns}
                )
            )
            application_feedback.update(
                self._repair_layer("ATOMIC", spec, before, state, eligible, previous_turns, runner)
            )
            state.feedback = self._verify_base(
                spec, before, state, eligible, application_feedback, runner
            )
            self._retain_safe_changes(state, before, runner)
            affected = {
                key
                for key, original in before.memories.items()
                if state.memories.get(key) != original
                or any(
                    state.turns.get(e.turn_id) != before.turns[e.turn_id] for e in original.evidence
                )
            }
            self.repository.load_experiences(
                user_id,
                before,
                state,
                affected | {key for layer, key in self._candidate_keys if layer == "ATOMIC"},
            )
            experience_plan = plan_experience_forgetting(
                spec,
                before,
                state.memories,
                state.turns,
                self.semantic,
                self._batches,
                self._eligible(spec, before),
                self.max_passes,
                source_revalidation=self.options.source_revalidation,
                runner=runner,
                previous_plan=experience_plan,
                previous_ids=previous_experience_ids,
            )
            previous_experience_ids = set(before.experiences)
            # Search the untouched store excluding ALL staged/checked records. Their
            # updated content, dates and deletions are verified above, never their old
            # database text. This is the staged overlay of the retrieval space.
            candidates = self._search_candidates(discovery, spec, user_id, runner)
            if not candidates and not state.feedback and not runner.retry_keys:
                break
        if candidates:
            # The budget may expire with a newly discovered page. Verify it without
            # silently claiming that unprocessed records have been forgotten.
            expanded = self.repository.expand(user_id, before, state, candidates)
            self._candidate_keys.update(expanded)
            state.feedback.update(
                self._verify_base(spec, before, state, self._eligible(spec, before), {}, runner)
            )
        if not discovery.exhausted:
            runner.failures[("discovery", "RAW", spec.request_id)] = "SEARCH_BUDGET_EXHAUSTED"
        return state, experience_plan

    def _search_candidates(self, discovery, spec, user_id, runner):
        key = ("discovery", "ATOMIC", spec.request_id)
        try:
            candidates = discovery.search(spec, user_id, seen=self._candidate_keys)
        except ForgettingError:
            runner.failures[key] = "RETRIEVAL_ERROR"
            # Preserve keyword/time discovery and all already successful repairs.
            embedding, discovery.embedding = discovery.embedding, None
            candidates = discovery.search(spec, user_id, seen=self._candidate_keys)
            discovery.embedding = embedding
        else:
            runner.failures.pop(key, None)
        return candidates

    def _retain_safe_changes(self, state, before, runner):
        # Keep last independently accepted versions for per-record fallback. Deletions
        # are represented by None, so a later rejection cannot resurrect safe deletions.
        for layer, originals, current, safe in (
            ("RAW", before.turns, state.turns, state.safe_turns),
            ("ATOMIC", before.memories, state.memories, state.safe_memories),
        ):
            for key in originals:
                if (
                    (layer, key) in self._candidate_keys
                    and (layer, key) not in state.feedback
                    and (layer, key) not in runner.retry_keys
                ):
                    safe[key] = current.get(key)
        if state.passes < self.max_passes:
            return
        # A failed preservation check must not authorize an over-deletion/invention.
        # Roll back only that record, retaining the rejection as next-pass feedback.
        rejected = {
            key for key, failure in state.feedback.items() if not failure["preservation_ok"]
        }
        rejected.update(
            (layer, key) for stage, layer, key in runner.failures if stage == "verification"
        )
        repaired_turns = dict(state.turns)
        restored_raw = False
        for layer, key in rejected:
            current = state.turns if layer == "RAW" else state.memories
            previous = state.safe_turns if layer == "RAW" else state.safe_memories
            if layer not in {"RAW", "ATOMIC"}:
                continue
            if previous.get(key) is not None:
                restored_raw |= layer == "RAW" and current.get(key) != previous[key]
                current[key] = previous[key]
            else:
                current.pop(key, None)
        if restored_raw:
            cascaded = set(state.orphaned)
            self._restore_orphans(state.orphaned, state.memories, state.turns)
            # A restored source can invalidate a previously passing atomic result.
            for key, original in before.memories.items():
                if any(("RAW", e.turn_id) in rejected for e in original.evidence):
                    safe = state.safe_memories.get(key)
                    if safe is not None:
                        state.memories[key] = safe
                    elif key not in cascaded:
                        state.memories.pop(key, None)
                    runner.failures[("verification", "ATOMIC", key)] = "SOURCE_REPAIR_REJECTED"
        if rejected:
            # Restored facts may refer to pre-redaction character positions. Keep
            # only message-level references until their support can be rechecked.
            for key, memory in list(state.memories.items()):
                if ("ATOMIC", key) in rejected or any(
                    ("RAW", e.turn_id) in rejected for e in memory.evidence
                ):
                    state.memories[key] = memory.model_copy(
                        update={
                            "evidence": [
                                e.model_copy(update={"span_start": None, "span_end": None})
                                for e in memory.evidence
                            ]
                        }
                    )
            self._repair_evidence(state.memories, state.turns, {**before.turns, **repaired_turns})

    def _repair_layer(self, layer, spec, before, state, eligible, previous_turns, runner):
        retry = (
            state.feedback.keys() | runner.retry_keys | state.pending if state.passes > 1 else set()
        )
        records = self._records(
            before, state.memories, state.turns, eligible, include_deleted=bool(retry)
        )
        records = [
            r
            for r in records
            if r["layer"] == layer
            and (
                not retry
                or (layer, r["id"]) in retry
                or (
                    layer == "ATOMIC"
                    and any(
                        previous_turns.get(e.turn_id) != state.turns.get(e.turn_id)
                        for e in before.memories[r["id"]].evidence
                    )
                )
            )
        ]
        for record in records:
            finding = state.feedback.get((layer, record["id"]))
            if finding is not None:
                record["verification_feedback"] = finding
        feedback = {}
        for batch in self._batches(records):
            result = runner.call("decision", batch, self.semantic.decide, spec, batch)
            if result is None:
                continue
            for decision in result.decisions:
                key = ("application", decision.layer, decision.id)
                try:
                    feedback.update(
                        self._apply_decisions(
                            batch, [decision], state.memories, state.turns, before
                        )
                    )
                except (ForgettingError, ValueError):
                    runner.failures[key] = "INVALID_REPAIR"
                    feedback[(decision.layer, decision.id)] = {
                        "operation_error": "Repair violated scope or record constraints"
                    }
                    continue
                runner.failures.pop(key, None)
                if decision.layer == "ATOMIC" and decision.operation == "DELETE":
                    state.orphaned.pop(decision.id, None)
        return feedback

    def _verify_base(self, spec, before, state, eligible, application_feedback, runner):
        records = self._records(before, state.memories, state.turns, eligible, include_deleted=True)
        keys = {
            (r["layer"], r["id"]): sha256(
                json.dumps(r, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            for r in records
        }
        unchecked = [
            r for r in records if keys[(r["layer"], r["id"])] not in state.verification_cache
        ]
        for batch in self._batches(unchecked):
            result = runner.call("verification", batch, self.semantic.verify, spec, batch)
            if result is not None:
                for finding in result.results:
                    state.verification_cache[keys[(finding.layer, finding.id)]] = finding
        feedback = {}
        for record in records:
            key = (record["layer"], record["id"])
            finding = state.verification_cache.get(keys[key])
            if finding is None:
                continue
            failed = repair_feedback(record, finding)
            if failed is not None:
                feedback[key] = {**failed, **application_feedback.get(key, {})}
            else:
                runner.verified(*key)
        return feedback

    def _commit(
        self,
        spec,
        user_id,
        before,
        memories,
        turns,
        replacement,
        experience_plan,
        profile_plan,
        failures,
        passes,
        feedback,
        operation_failures,
    ):
        profile_file_synced = self.repository.apply(
            user_id,
            before,
            memories,
            turns,
            new_session=replacement.session,
            experiences=experience_plan.experiences,
            profiles=profile_plan.entries,
        )
        report = ForgetReport(
            request_id=spec.request_id,
            scope=spec.scope,
            verification_scope="retrieved_and_linked" if before.partial else "full_snapshot",
            deleted_memories=len(before.memories.keys() - memories.keys()),
            redacted_memories=sum(
                m.content != before.memories[key].content
                or m.valid_time != before.memories[key].valid_time
                for key, m in memories.items()
                if key in before.memories
            ),
            deleted_turns=len(before.turns.keys() - turns.keys()),
            redacted_turns=sum(
                t.content != before.turns[key].content
                for key, t in turns.items()
                if key in before.turns
            ),
            repaired_evidence=sum(
                m.evidence != before.memories[key].evidence
                for key, m in memories.items()
                if key in before.memories
            ),
            verification_passes=passes,
            verified=not failures and not operation_failures and profile_file_synced is not False,
            storage_verified=profile_file_synced is not False,
            profile_file_synced=profile_file_synced,
            semantic_verified=not failures and not operation_failures,
            operation_failures=operation_failures,
            dense_invalidated=True,
            replacement_memories=len(replacement.memories),
            verification_failures=failures,
            clarification_recovered=spec.clarification_recovered,
            deleted_experiences=len(before.experiences.keys() - experience_plan.experiences.keys()),
            redacted_experiences=sum(
                (e.content, e.valid_time)
                != (before.experiences[key].content, before.experiences[key].valid_time)
                for key, e in experience_plan.experiences.items()
            ),
            experience_verification_passes=experience_plan.passes,
            deleted_profile_entries=len(before.profiles.keys() - profile_plan.entries.keys()),
            regenerated_profile_entries=sum(
                entry.content != before.profiles[key].content
                for key, entry in profile_plan.entries.items()
            ),
            repair_cost=asdict(repair_cost(before, memories, turns, feedback)),
        )
        report.probe_results = run_probes(
            self.semantic, spec, before, self.probe_count, self.probe_answer
        )
        if any(not result["passed"] for result in report.probe_results):
            report.verified = False
        if report.probe_results:
            report.probe_verified = all(result["passed"] for result in report.probe_results)
        return report

    @staticmethod
    def _restore_orphans(orphaned, memories, turns):
        # Only automatic cascades may be restored. Explicit semantic deletions are removed
        # from orphaned as soon as they are planned.
        for key, memory in list(orphaned.items()):
            if key not in memories and any(e.turn_id in turns for e in memory.evidence):
                memories[key] = memory.model_copy(
                    update={
                        "evidence": [
                            e.model_copy(update={"span_start": None, "span_end": None})
                            for e in memory.evidence
                        ]
                    }
                )
                del orphaned[key]

    def _eligible(self, spec: ForgetSpec, before: ForgetSnapshot) -> set[tuple[str, str]]:
        if not (spec.time_start or spec.time_end):
            return {("ATOMIC", key) for key in before.memories} | {
                ("RAW", key) for key in before.turns
            }
        lower = date_bounds(spec.time_start)[0] if spec.time_start else date.min
        upper = date_bounds(spec.time_end)[1] if spec.time_end else date.max
        if spec.time_field == "RECORDED_TIME":
            raw = {
                key
                for key, turn in before.turns.items()
                if lower <= turn.timestamp.astimezone(UTC).date() <= upper
            }
            atoms = {
                key
                for key, memory in before.memories.items()
                if lower <= memory.created_at.astimezone(UTC).date() <= upper
                or any(e.turn_id in raw for e in memory.evidence)
            }
            return {("RAW", key) for key in raw} | {("ATOMIC", key) for key in atoms}
        result = {("RAW", key) for key in before.turns}
        for key, memory in before.memories.items():
            time = memory.valid_time
            if time is None:
                result.add(("ATOMIC", key))
                continue
            start = time.point or time.start
            end = time.point or time.end
            if (date_bounds(start)[0] if start else date.min) <= upper and (
                date_bounds(end)[1] if end else date.max
            ) >= lower:
                result.add(("ATOMIC", key))
        return result

    def _records(self, before, memories, turns, eligible, *, include_deleted):
        records = []
        by_session = {}
        for key, original in before.turns.items():
            by_session.setdefault(original.session_id, []).append(key)
        neighbors = {}
        for keys in by_session.values():
            for i, key in enumerate(keys):
                neighbors[key] = [
                    turns[other].model_dump(mode="json")
                    for other in keys[max(0, i - 2) : i + 3]
                    if other != key and other in turns
                ]
        for layer, originals, current in (
            ("ATOMIC", before.memories, memories),
            ("RAW", before.turns, turns),
        ):
            for key, original in originals.items():
                if self._candidate_keys is not None and (layer, key) not in self._candidate_keys:
                    continue
                item = current.get(key)
                if item is None and not include_deleted:
                    continue
                record = {
                    "layer": layer,
                    "id": key,
                    "eligible": (layer, key) in eligible,
                    "content": item.content if item else None,
                    "original_content": original.content,
                    "original_valid_time": original.valid_time.model_dump()
                    if layer == "ATOMIC" and original.valid_time
                    else None,
                    "valid_time": item.valid_time.model_dump()
                    if layer == "ATOMIC" and item and item.valid_time
                    else None,
                }
                if layer == "RAW":
                    record.update(
                        role=original.role,
                        timestamp=original.timestamp.isoformat(),
                        context=neighbors[key],
                    )
                else:
                    source_memory = item or original
                    # Revalidate support when this operation changes a claim or its
                    # evidence. Re-auditing unrelated, byte-identical claims can block
                    # deletion on pre-existing extraction errors or missing context.
                    source_ids = {e.turn_id for e in original.evidence} | {
                        e.turn_id for e in source_memory.evidence
                    }
                    record["support_check_required"] = (
                        self.options.source_revalidation
                        and item is not None
                        and (
                            item != original
                            or any(turns.get(key) != before.turns[key] for key in source_ids)
                        )
                    )
                    record["remaining_sources"] = [
                        turns[e.turn_id].model_dump(mode="json")
                        for e in source_memory.evidence
                        if e.turn_id in turns
                    ]
                    record["created_at"] = original.created_at.isoformat()
                    record["status"] = original.status
                records.append(record)
        return records

    def _batches(self, records):
        batch, size = [], 0
        for record in records:
            length = len(json.dumps(record, ensure_ascii=False))
            if batch and (len(batch) >= self.batch_size or size + length > self.batch_chars):
                yield batch
                batch, size = [], 0
            batch.append(record)
            size += length
        if batch:
            yield batch

    def _apply_decisions(self, records, decisions, memories, turns, before):
        inputs = {(record["layer"], record["id"]): record for record in records}
        feedback = {}
        for decision in decisions:
            record = inputs[(decision.layer, decision.id)]
            if not record["eligible"] and decision.operation != "KEEP":
                raise ForgettingError("Forgetting decision exceeds the requested time scope")
            if decision.operation == "KEEP":
                continue
            collection = memories if decision.layer == "ATOMIC" else turns
            if decision.operation == "DELETE":
                collection.pop(decision.id, None)
                continue
            updates = {"content": decision.new_content}
            if decision.layer == "ATOMIC":
                time = decision.new_valid_time
                if time is not None:
                    retained_time = (
                        record["valid_time"]
                        if decision.id in collection
                        else record["original_valid_time"]
                    )
                    allowed = set((retained_time or {}).values()) | {None}
                    if any(value not in allowed for value in time.model_dump().values()):
                        raise ForgettingError("A forgetting decision invented event dates")
                updates["valid_time"] = (
                    time.model_dump() if time and any(time.model_dump().values()) else None
                )
            current = collection.get(decision.id)
            if current is None:
                originals = before.memories if decision.layer == "ATOMIC" else before.turns
                current = originals[decision.id]
                if decision.layer == "ATOMIC":
                    # Restored claims cite surviving turns; old character offsets may
                    # refer to text redacted in a previous pass.
                    updates["evidence"] = [
                        e.model_copy(update={"span_start": None, "span_end": None})
                        for e in current.evidence
                    ]
            updated = type(current).model_validate({**current.model_dump(), **updates})
            if decision.id in collection and current == updated:
                # Let verification distinguish harmless KEEP from an unsuccessful repair.
                # If the record still fails, feed this operation error into the next pass.
                feedback[(decision.layer, decision.id)] = {
                    "operation_error": "REDACT did not change its target record"
                }
                continue
            collection[decision.id] = updated
        return feedback

    def _repair_evidence(self, memories, turns, previous_turns):
        matching_blocks = {}
        orphaned = {}
        for key, memory in list(memories.items()):
            sources = []
            for evidence in memory.evidence:
                if evidence.turn_id not in turns:
                    continue
                original = previous_turns[evidence.turn_id]
                current = turns[evidence.turn_id]
                start, end = evidence.span_start, evidence.span_end
                if current.content != original.content and start is not None:
                    mapped = None
                    if evidence.turn_id not in matching_blocks:
                        matching_blocks[evidence.turn_id] = SequenceMatcher(
                            None, original.content, current.content, autojunk=False
                        ).get_matching_blocks()
                    for block in matching_blocks[evidence.turn_id]:
                        if block.a <= start and end <= block.a + block.size:
                            mapped = (block.b + start - block.a, block.b + end - block.a)
                            break
                    start, end = mapped if mapped else (None, None)
                sources.append(
                    Evidence(
                        session_id=evidence.session_id,
                        turn_id=evidence.turn_id,
                        span_start=start,
                        span_end=end,
                    )
                )
            if not sources:
                orphaned[key] = memory
                del memories[key]
            else:
                unique = list({(e.turn_id, e.span_start, e.span_end): e for e in sources}.values())
                memories[key] = memory.model_copy(update={"evidence": unique})
        return orphaned
