from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "custom_components" / "roborock_plus" / "coordinator.py"
VACUUM = ROOT / "custom_components" / "roborock_plus" / "vacuum.py"


def test_coordinator_keeps_last_resume_command_in_memory_only() -> None:
    coordinator = COORDINATOR.read_text(encoding="utf-8")

    assert "self.last_resume_command: str | None = None" in coordinator


def test_v1_pause_records_resume_command_before_app_pause() -> None:
    vacuum = VACUUM.read_text(encoding="utf-8")

    pause_body = vacuum.split("async def async_pause(self) -> None:", 1)[1].split(
        "async def async_stop", 1
    )[0]

    assert "select_resume_command_from_status" in pause_body
    assert "self.coordinator.last_resume_command" in pause_body
    assert pause_body.index("self.coordinator.last_resume_command") < pause_body.index(
        "RoborockCommand.APP_PAUSE"
    )


def test_v1_resume_task_uses_saved_context_and_never_start() -> None:
    vacuum = VACUUM.read_text(encoding="utf-8")

    resume_body = vacuum.split("async def async_resume_task(self) -> None:", 1)[1].split(
        "async def async_pause", 1
    )[0]

    assert "fallback_command=self.coordinator.last_resume_command" in resume_body
    assert "self.coordinator.last_resume_command = None" in resume_body
    assert "APP_START" not in resume_body
    assert "app_start" not in resume_body
