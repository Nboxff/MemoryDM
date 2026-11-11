"""Recover model failures per batch without weakening storage or scope validation."""

from memory_dm.forgetting.models import ForgetResponseError, VerificationItem
from memory_dm.llm.clients import ClientError
from memory_dm.llm.structured import StructuredResponseError


def verification_items(feedback):
    return [
        VerificationItem(
            layer=layer,
            id=key,
            **{field: value[field] for field in ("target_present", "preservation_ok", "supported")},
        )
        for (layer, key), value in feedback.items()
    ]


class SemanticBatches:
    def __init__(self):
        self.failures = {}

    @property
    def retry_keys(self):
        return {(layer, key) for _, layer, key in self.failures}

    def call(self, stage, records, operation, *args):
        keys = {(stage, r["layer"], r["id"]) for r in records}
        try:
            result = operation(*args)
        except (ForgetResponseError, StructuredResponseError, ClientError) as error:
            code = "API_ERROR" if isinstance(error, ClientError) else "SCHEMA_ERROR"
            self.failures.update({key: code for key in keys})
            return None
        for key in keys:
            self.failures.pop(key, None)
        return result

    def verified(self, layer, key):
        # A successful target/preservation check can establish that an unchanged
        # record needed no edit after a decision response was unavailable.
        self.failures.pop(("decision", layer, key), None)
        self.failures.pop(("verification", layer, key), None)

    def public(self):
        return [
            {"stage": stage, "layer": layer, "id": key, "error_code": code}
            for (stage, layer, key), code in sorted(self.failures.items())
        ]
