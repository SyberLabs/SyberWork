"""API-level refusals. These are not admission decisions."""


class Rejected(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")
