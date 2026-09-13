"""生图结果状态契约测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kantoku.schemas.media import ImageGenerationResult


def test_succeeded_result_requires_path_and_provider_job_id() -> None:
    result = ImageGenerationResult(
        path=Path("result.png"),
        provider_job_id="provider-1",
        status="succeeded",
        actual_fen=30,
    )

    assert result.actual_fen == 30
    with pytest.raises(ValidationError):
        ImageGenerationResult(status="succeeded", actual_fen=30)


def test_failed_and_unknown_states_cannot_claim_invalid_outputs() -> None:
    with pytest.raises(ValidationError):
        ImageGenerationResult(
            path=Path("should-not-exist.png"),
            provider_job_id="provider-1",
            status="failed",
        )
    with pytest.raises(ValidationError):
        ImageGenerationResult(status="unknown", actual_fen=0)
    with pytest.raises(ValidationError):
        ImageGenerationResult(status="failed", actual_fen=True)
