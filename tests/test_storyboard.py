from __future__ import annotations

import json
import traceback

import pytest

from kantoku.config import SchemaError
from kantoku.schemas.storyboard import EXAMPLE_SHOT_JSON, Storyboard, parse_storyboard


def _board() -> dict[str, object]:
    return {
        "episode": "雨夜重逢",
        "shots": [
            {
                "shot_no": 1,
                "desc": "雨落在空街",
                "camera": "远景",
                "duration_s": 3,
                "characters": [],
            }
        ],
    }


def test_storyboard_json_roundtrip() -> None:
    board = parse_storyboard(json.dumps(_board(), ensure_ascii=False))
    assert board.shots[0].dialogue == ""
    assert board.shots[0].characters == []
    assert parse_storyboard(board.model_dump_json()) == board
    assert "shots" in Storyboard.model_json_schema()["required"]
    assert len(parse_storyboard(EXAMPLE_SHOT_JSON).shots) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("shot_no", -1),
        ("shot_no", True),
        ("shot_no", "1"),
        ("duration_s", 0),
        ("duration_s", 1.5),
        ("desc", "  "),
        ("camera", ""),
        ("characters", [""]),
        ("unexpected", "value"),
    ],
)
def test_storyboard_rejects_invalid_shot(field: str, value: object) -> None:
    data = _board()
    data["shots"][0][field] = value
    with pytest.raises(SchemaError):
        parse_storyboard(json.dumps(data))


@pytest.mark.parametrize("numbers", [[], [2], [1, 1], [1, 3], [2, 1]])
def test_storyboard_requires_nonempty_contiguous_order(numbers: list[int]) -> None:
    data = _board()
    template = data["shots"][0]
    data["shots"] = [dict(template, shot_no=number) for number in numbers]
    with pytest.raises(SchemaError):
        parse_storyboard(json.dumps(data))


@pytest.mark.parametrize("raw", ["not-json", '{"private-script":"不要公开"}'])
def test_parse_error_keeps_private_raw_out_of_display(raw: str) -> None:
    with pytest.raises(SchemaError) as caught:
        parse_storyboard(raw)
    assert caught.value.raw == raw
    assert raw not in str(caught.value)
    assert raw not in "".join(traceback.format_exception(caught.value))
