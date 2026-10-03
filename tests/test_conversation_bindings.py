"""Conversation history recovers explicit Run bindings without project/title inference."""

from pathlib import Path

from test_web_studio import app as app_fixture

from kantoku.core.runtime.store import RuntimeStore
from kantoku.shells.web_studio import StudioApplication

app = app_fixture


def test_new_conversation_has_no_old_runs_or_messages(app: StudioApplication) -> None:
    old = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})
    run = app.runtime_store.create_run(
        "comic", "comic.director", {"conversation_id": old["id"], "project_id": "old"},
        "creative_understanding",
    )
    new = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})
    assert new["id"] != old["id"]
    assert app.conversation(new["id"])["related_run_ids"] == []
    assert app.conversation(new["id"])["messages"] == []
    assert app.conversation(old["id"])["related_run_ids"] == [run.id]


def test_bindings_survive_restart_and_do_not_infer_from_shared_project(
    app: StudioApplication,
) -> None:
    old = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})
    new = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})
    old_run = app.runtime_store.create_run(
        "comic", "comic.director", {"project_id": "shared", "conversation_id": old["id"]},
        "creative_understanding",
    )
    new_run = app.runtime_store.create_run(
        "comic", "comic.director", {"project_id": "shared", "conversation_id": new["id"]},
        "creative_understanding",
    )
    app.runtime_store.create_run("comic", "comic.director", {"project_id": "shared"}, "draft")
    restarted = StudioApplication()
    assert restarted.conversation(old["id"])["related_run_ids"] == [old_run.id]
    assert restarted.conversation(new["id"])["related_run_ids"] == [new_run.id]
    app.rename_conversation(new["id"], "独立的新创意")
    assert restarted.conversation(old["id"])["title"] == "新对话"
    assert restarted.conversation(new["id"])["title"] == "独立的新创意"
    app.delete_conversation(new["id"])
    assert restarted.runtime_store.get_run(new_run.id).id == new_run.id
    assert [item["id"] for item in restarted.list_conversations(domain="comic")] == [old["id"]]


def test_conversation_filter_is_applied_before_the_run_limit(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    own = store.create_run("comic", "comic.director", {"conversation_id": "mine"}, "draft")
    for _ in range(3):
        store.create_run("comic", "comic.director", {"conversation_id": "another"}, "draft")
    assert [run.id for run in store.list_runs(limit=1, conversation_id="mine")] == [own.id]
