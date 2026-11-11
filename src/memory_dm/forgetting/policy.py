"""Explicit experiment policies, separate from source-integrity constraints."""

from memory_dm.forgetting.semantic import SemanticForgetter


class PolicyForgetter(SemanticForgetter):
    def __init__(self, client, options):
        super().__init__(client)
        self.options = options

    def decide(self, spec, records):
        result = super().decide(spec, records)
        decisions = []
        for decision in result.decisions:
            rewrite = decision.operation in {"REDACT", "REGENERATE"}
            derived = decision.layer in {"EXPERIENCE", "PROFILE"}
            if rewrite and (
                (derived and not self.options.regenerate)
                or (not derived and not self.options.local_rewrite)
            ):
                decision = decision.model_copy(
                    update={"operation": "DELETE", "new_content": None, "new_valid_time": None}
                )
            decisions.append(decision)
        return result.model_copy(update={"decisions": decisions})
