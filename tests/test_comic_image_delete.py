"""导入图片的删除：整张图片（含全部版本）从作品里移除。

图片不是"只增不删"的 Prompt 版本，删掉是用户撤回一次错误导入的唯一出口，
因此必须同时守住三条边界：
① 删除只影响目标图片，其他图片、采用指针与工作台偏好不被牵连；
② 故事已固定、或正作为参考资产使用的图片必须被拒绝（删了会让下游失去来源）；
③ 作品级乐观并发与请求幂等都要生效，删除本身计入一次作品修订。
"""

import base64
import json
import struct
import zlib
from http.client import HTTPConnection

import pytest
from test_web_studio import app as app_fixture
from test_web_studio import server as server_fixture

from kantoku.config import ToolError
from kantoku.core.runtime.models import ArtifactLifecycle, utc_now
from kantoku.shells import web_studio

app = app_fixture
server = server_fixture


def _png(color=0):
    def chunk(name, content):
        return (
            struct.pack(">I", len(content))
            + name
            + content
            + struct.pack(">I", zlib.crc32(name + content))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, color, 0, 0])))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def media(app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "output", raising=False
    )
    monkeypatch.setattr(
        app, "_comic_constraint_model", lambda _: pytest.fail("导图与删除不能调用模型")
    )
    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    pid = app.create_comic_project({"title": "删除导入图片"})["project"]["project_id"]
    return app, pid, cid


def _upload(app, pid, cid, request_id="upload", color=0, **changes):
    data = {
        "request_id": request_id,
        "conversation_id": cid,
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        "filename": "portrait.png",
        "data_url": "data:image/png;base64," + base64.b64encode(_png(color)).decode(),
        **changes,
    }
    return app.import_comic_image(pid, data)


def _revision(app, pid, cid, image, request_id="revision"):
    return _upload(
        app, pid, cid, request_id, color=120,
        image_id=image["image_id"], expected_image_version=1,
        parent={"image_id": image["image_id"], "version": 1},
    )


def _delete(app, pid, cid, image_id, request_id="delete", **changes):
    return app.delete_comic_image(pid, image_id, {
        "request_id": request_id,
        "conversation_id": cid,
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        **changes,
    })


def test_delete_removes_every_version_and_trashes_its_artifacts(media):
    app, pid, cid = media
    image = _upload(app, pid, cid)
    revised = _revision(app, pid, cid, image)
    assert [item["version"] for item in app.list_comic_images(pid, cid)["images"]] == [2]

    result = _delete(app, pid, cid, image["image_id"])

    assert result["image_id"] == image["image_id"]
    assert result["deleted_versions"] == [1, 2]
    assert app.list_comic_images(pid, cid)["images"] == []
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        app.comic_media.get(image["image_id"])
    for artifact_id in (image["artifact_id"], revised["artifact_id"]):
        artifact = app.runtime_store.get_artifact(artifact_id)
        assert artifact.lifecycle_state == ArtifactLifecycle.TRASHED
        # 文件留给回收站，删除本身不销毁媒体。
        assert artifact.location


def test_delete_keeps_other_images_and_counts_as_one_project_revision(media):
    app, pid, cid = media
    kept = _upload(app, pid, cid, "keep")
    removed = _upload(app, pid, cid, "remove", color=90)
    before = app.comic_projects.get(pid).project.current_version

    result = _delete(app, pid, cid, removed["image_id"])

    assert result["project_version"] == before + 1
    assert app.comic_projects.get(pid).project.current_version == before + 1
    assert [item["image_id"] for item in app.list_comic_images(pid, cid)["images"]] == [
        kept["image_id"]
    ]


def test_delete_refuses_image_fixed_by_story(media):
    app, pid, cid = media
    image = _upload(app, pid, cid)
    version = app.comic_projects.get(pid).project.current_version
    # 故事只记录被固定的图片版本；这里按真实 payload 结构写入同一张表。
    with app.comic_projects._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        app.comic_projects._insert_version(
            connection, project_id=pid, entity_type="story", entity_id="story-1",
            version=1, created_at=utc_now(), payload_json=json.dumps({
                "story_id": "story-1", "version": 1,
                "fixed_images": [{"image_id": image["image_id"], "version": 1,
                                  "artifact_id": image["artifact_id"], "project_id": pid}],
                "shots": [],
            }),
        )
        app.comic_projects._advance_project(connection, pid, version)

    with pytest.raises(ToolError, match="已在故事编排里固定"):
        _delete(app, pid, cid, image["image_id"])

    # 被拒绝的删除不得留下副作用。
    assert [item["image_id"] for item in app.list_comic_images(pid, cid)["images"]] == [
        image["image_id"]
    ]
    assert app.runtime_store.get_artifact(
        image["artifact_id"]).lifecycle_state == ArtifactLifecycle.ACTIVE


def test_delete_refuses_image_bound_as_reference(media):
    app, pid, cid = media
    image = _upload(app, pid, cid)
    app.bind_comic_references(pid, {
        "request_id": "bind",
        "conversation_id": cid,
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        "expected_reference_version": 0,
        "bindings": [{
            "image": {"image_id": image["image_id"], "version": 1},
            "roles": ["identity"], "target": "主角",
        }],
    })

    with pytest.raises(ToolError, match="正作为参考资产使用"):
        _delete(app, pid, cid, image["image_id"])

    assert app.comic_projects.get(pid).project.current_version == image["project_version"] + 1


def test_delete_rejects_stale_project_version_and_replays_same_request(media):
    app, pid, cid = media
    image = _upload(app, pid, cid)
    with pytest.raises(ToolError, match="作品已由其他操作更新"):
        _delete(app, pid, cid, image["image_id"],
                expected_project_version=image["project_version"] + 5)
    assert [item["image_id"] for item in app.list_comic_images(pid, cid)["images"]] == [
        image["image_id"]
    ]

    first = _delete(app, pid, cid, image["image_id"])
    # 同一个 request_id 重放返回原结果，不再追加一次作品修订。
    replayed = _delete(app, pid, cid, image["image_id"],
                       expected_project_version=image["project_version"])
    assert replayed == first
    assert app.comic_projects.get(pid).project.current_version == first["project_version"]


def test_delete_route_removes_image_over_http(media, server):
    app, pid, cid = media
    image = _upload(app, pid, cid)
    connection = HTTPConnection("127.0.0.1", server)
    try:
        connection.request(
            "POST", f"/api/comic/projects/{pid}/images/{image['image_id']}/delete",
            body=json.dumps({
                "request_id": "http-delete", "conversation_id": cid,
                "expected_project_version": app.comic_projects.get(pid).project.current_version,
            }),
            headers={"X-Studio-Token": app.token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200, payload
        assert payload["deleted_versions"] == [1]
        connection.request(
            "GET", f"/api/comic/projects/{pid}/images?conversation_id={cid}",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        assert json.loads(response.read()) == {"images": []}
    finally:
        connection.close()
