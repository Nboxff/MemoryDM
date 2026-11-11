"""Ground query-local person references without inventing a persistent user identity."""

from memory_dm.llm.structured import ResponseValidationError
from memory_dm.models import Turn
from memory_dm.retrieval.models import AttributeCoverage, QuerySubject


def owner_scope(user_id: str | None) -> dict:
    return {"owner": "CURRENT_USER", "user_id": user_id, "user_id_is_name_evidence": False}


def validate_subjects(
    subjects: list[QuerySubject],
    query: str,
    history: list[Turn],
    records: list[dict] | None = None,
) -> None:
    ids = [subject.id for subject in subjects]
    if len(set(ids)) != len(ids):
        raise ResponseValidationError("Query subject IDs must be unique")
    sources = {(item["layer"], item["id"]): item["content"] for item in records or []}
    assertions = {
        (item["layer"], item["id"])
        for item in records or []
        if item["layer"] in {"ATOMIC", "EXPERIENCE", "PROFILE"} or item.get("role") == "user"
    }
    # The question can specify whom to ask about, but cannot independently establish
    # a stored identity merely by naming someone. Prior user assertions remain evidence.
    sources.update(
        {
            ("WORKING", turn.turn_id): turn.content
            for turn in history
            if not (turn.role == "user" and turn.content == query)
        }
    )
    assertions.update(
        ("WORKING", turn.turn_id)
        for turn in history
        if turn.role == "user" and turn.content != query
    )
    for subject in subjects:
        if subject.mention is not None and subject.mention not in query:
            raise ResponseValidationError("Copy each subject mention exactly from the query")
        if subject.query_quote is not None and subject.query_quote not in query:
            raise ResponseValidationError("Subject query quotes must appear in the original query")
        if subject.basis == "FIRST_PERSON" and subject.mention is None:
            raise ResponseValidationError("Copy the first-person subject reference from the query")
        for reference in subject.evidence:
            content = sources.get((reference.layer, reference.id))
            if content is None or reference.quote not in content:
                raise ResponseValidationError(
                    "Identity evidence must cite an available source and an exact quote; "
                    "the current question and opaque user_id are not identity evidence"
                )
        if subject.basis in {"MEMORY", "OWNER_SCOPE"} and not any(
            (reference.layer, reference.id) in assertions for reference in subject.evidence
        ):
            raise ResponseValidationError(
                "Assistant-only statements cannot establish an identity or owner-scoped referent"
            )


def validate_subject_coverage(
    original: list[QuerySubject],
    resolved: list[QuerySubject],
    coverage: list[AttributeCoverage],
    *,
    query_turn_ids: set[str],
) -> None:
    expected = {subject.id: subject.mention for subject in original}
    actual = {subject.id: subject.mention for subject in resolved}
    if expected != actual:
        raise ResponseValidationError(
            "Preserve the original subject IDs and mentions; resolve them using query context "
            "or grounded memory evidence rather than dropping ambiguous people"
        )
    by_id = {subject.id: subject for subject in resolved}
    # IDs anchor the question's references, not the planner's interpretation of them.
    # The evaluator can correct a mistaken OTHER_PERSON/UNRESOLVED classification
    # using query context or visible records; validate_subjects checks that grounding.
    owner_labels = {subject.mention for subject in resolved if subject.basis == "OWNER_SCOPE"}
    if len(owner_labels) > 1:
        raise ResponseValidationError(
            "Do not collapse multiple independently named people into the memory owner. "
            "Resolve their relationships with actual evidence or keep the ambiguity."
        )
    for attribute in coverage:
        if len(set(attribute.subject_ids)) != len(attribute.subject_ids):
            raise ResponseValidationError("Coverage subject IDs must be unique")
        if set(attribute.subject_ids) - by_id.keys():
            raise ResponseValidationError("Coverage may reference only the resolved query subjects")
        if by_id and attribute.role == "ANSWER" and not attribute.subject_ids:
            raise ResponseValidationError(
                "Associate each requested personal attribute with its subjects"
            )
        if attribute.status != "MISSING" and any(
            by_id[key].target == "UNRESOLVED" for key in attribute.subject_ids
        ):
            raise ResponseValidationError(
                "Unresolved identity cannot support a personal attribute; keep it MISSING "
                "or supply grounded identity evidence"
            )
        if by_id and attribute.status == "MISSING":
            unresolved = any(by_id[key].target == "UNRESOLVED" for key in attribute.subject_ids)
            if attribute.missing_reason is None:
                raise ResponseValidationError(
                    "Distinguish MISSING identity (IDENTITY) from a missing fact (ATTRIBUTE)"
                )
            if attribute.missing_reason == "IDENTITY" and not unresolved:
                raise ResponseValidationError(
                    "The query subjects are already resolved. A query-local owner reference "
                    "does not require a stored real name. Recheck the requested attribute "
                    "against that person's records. Missing name evidence makes the name "
                    "attribute unknown, not every other attribute."
                )
        aliases = {
            by_id[key].mention
            for key in attribute.subject_ids
            if by_id[key].basis in {"QUERY_CONTEXT", "OWNER_SCOPE"}
            and by_id[key].target == "CURRENT_USER"
        }
        if any(
            reference.layer == "WORKING"
            and reference.id in query_turn_ids
            and reference.quote in aliases
            for reference in attribute.evidence
        ):
            raise ResponseValidationError(
                "A query-local alias identifies the subject; "
                "it does not prove a name or any attribute"
            )
