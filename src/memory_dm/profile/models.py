"""One bounded Markdown document per user; profiles have no provenance graph."""

from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from memory_dm.models import Record, utc_now
from memory_dm.profile.tokens import PROFILE_TOKEN_LIMIT, token_count


class UserProfile(Record):
    user_id: str
    content: str
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def bounded_document(self):
        if token_count(self.content) > PROFILE_TOKEN_LIMIT:
            raise ValueError("The complete profile must fit within 1200 cl100k_base tokens")
        return self


class ProfileUpdate(Record):
    # Null means no useful profile, rather than an invented filler document.
    markdown: str | None
