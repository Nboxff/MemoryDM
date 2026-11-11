"""Compare fully evaluated repair passes without claiming global semantic optimality."""

from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True, order=True)
class RepairCost:
    targets_remaining: int
    unsupported_records: int
    preservation_failures: int
    deleted_records: int
    changed_characters: int


def repair_cost(before, memories, turns, feedback) -> RepairCost:
    def differences(originals, current):
        deleted = len(originals.keys() - current.keys())
        changed = sum(len(v.content) for key, v in originals.items() if key not in current)
        for key in originals.keys() & current.keys():
            old, new = originals[key].content, current[key].content
            if old != new:
                changed += sum(
                    max(a2 - a1, b2 - b1)
                    for op, a1, a2, b1, b2 in SequenceMatcher(
                        None, old, new, autojunk=False
                    ).get_opcodes()
                    if op != "equal"
                )
        return deleted, changed

    atom, raw = differences(before.memories, memories), differences(before.turns, turns)
    return RepairCost(
        sum(bool(f["target_present"]) for f in feedback.values()),
        sum(not f["supported"] for f in feedback.values()),
        sum(not f["preservation_ok"] for f in feedback.values()),
        atom[0] + raw[0],
        atom[1] + raw[1],
    )
