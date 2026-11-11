from memory_dm.models import AtomicMemory, Turn, ValidTime


class MemoryValidator:
    # Validate source identity before normalizing model-generated character offsets.
    def validate(self, memory: AtomicMemory, turns: list[Turn]) -> AtomicMemory:
        memory = AtomicMemory.model_validate(memory.model_dump())
        sources = {(turn.session_id, turn.turn_id): turn for turn in turns}
        for evidence in memory.evidence:
            source = sources.get((evidence.session_id, evidence.turn_id))
            if source is None:
                raise ValueError("Evidence must reference an available source turn")
            if evidence.span_end is not None:
                evidence.span_end = min(evidence.span_end, len(source.content))
                if evidence.span_start >= evidence.span_end:
                    # An empty intersection retains turn-level evidence, with no invented span.
                    evidence.span_start = evidence.span_end = None
        return memory

    def validate_batch(self, memories: list[AtomicMemory], turns: list[Turn]) -> list[AtomicMemory]:
        unique = {}
        for memory in memories:
            memory = self.validate(memory, turns)
            time = memory.valid_time or ValidTime()
            key = (memory.user_id, memory.type, memory.content, time.start, time.end, time.point)
            if key not in unique:
                unique[key] = memory.model_copy(update={"evidence": []})
            for evidence in memory.evidence:
                if evidence not in unique[key].evidence:
                    unique[key].evidence.append(evidence)
        return list(unique.values())
