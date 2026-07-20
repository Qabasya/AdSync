"""In-memory фейки Protocol-интерфейсов для тестов (без сети, без реального AD)."""

from models import AckRequest, Job


class FakeLmsApi:
    """Реализация `LmsApi` в памяти: отдаёт заранее заданные задания, копит ack-и."""

    def __init__(
        self,
        jobs: list[Job] | None = None,
        active_usernames: list[str] | None = None,
    ) -> None:
        self.jobs: list[Job] = jobs if jobs is not None else []
        self.active_usernames: list[str] = active_usernames if active_usernames is not None else []
        self.acks: list[AckRequest] = []

    def get_jobs(self, limit: int) -> list[Job]:
        return self.jobs[:limit]

    def ack(self, request: AckRequest) -> None:
        self.acks.append(request)

    def get_active_usernames(self) -> list[str]:
        return self.active_usernames
