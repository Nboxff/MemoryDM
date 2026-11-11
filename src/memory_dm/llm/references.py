"""Short, request-local references; stored IDs and ownership checks stay unchanged."""


class References:
    def __init__(self, identifiers, prefix: str, *, reserved=()):
        identifiers = tuple(dict.fromkeys(identifiers))
        occupied = set(identifiers) | set(reserved)
        self.encoded = {}
        index = 1
        for key in identifiers:
            while f"{prefix}{index}" in occupied:
                index += 1
            self.encoded[key] = f"{prefix}{index}"
            index += 1
        self.decoded = {value: key for key, value in self.encoded.items()}

    def encode(self, identifier: str) -> str:
        return self.encoded.get(identifier, identifier)

    def decode(self, identifier: str) -> str:
        # Unknown references are left invalid so the caller's normal scope validator
        # rejects them. They are never guessed, dropped, or converted into ADD.
        return self.decoded.get(identifier, identifier)
