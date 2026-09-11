from pathlib import Path

import pytest

from knowledge_distiller.v1.domain import (
    CapturedMaterial,
    Evidence,
    Knowledge,
    Point,
    SourceFact,
)
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.publisher import PublicationState, publish
from knowledge_distiller.v1.store import Store


def prepared(tmp_path: Path) -> tuple[Store, int, Path]:
    store = Store(tmp_path / "knowledge.sqlite3")
    store.initialize()
    media = tmp_path / "source.mp4"
    media.write_bytes(b"media")
    item_id = store.create_item("https://v.douyin.com/a/")
    material_id = store.attach_material(
        item_id,
        CapturedMaterial(
            "douyin",
            "123",
            "https://v.douyin.com/a/",
            "https://www.douyin.com/video/123",
            {"author": {"display_name": "测试作者"}},
            media,
            12.0,
        ),
    )
    snapshot = "持续切换会带来额外损耗。"
    source_fact_id = store.establish_source_fact(material_id, SourceFact(snapshot))
    store.establish_knowledge(
        source_fact_id,
        Knowledge(
            "注意力需要边界",
            "说明边界如何保护有限注意力。",
            "主动设定边界可以减少注意力损耗。",
            (
                Point(
                    "p1",
                    "边界保护注意力。",
                    "持续切换会带来额外损耗。",
                    ("e1",),
                ),
            ),
            (),
            (Evidence("e1", 0, 7, "持续切换会带来"),),
        ),
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    return store, item_id, vault


def test_publish_places_complete_file_before_recording_success(tmp_path: Path) -> None:
    store, item_id, vault = prepared(tmp_path)

    result = publish(store, item_id, vault)

    assert result.state is PublicationState.PUBLISHED
    target = vault / result.relative_path
    assert target.is_file()
    assert "# 注意力需要边界" in target.read_text(encoding="utf-8")
    assert store.item_bundle(item_id)["published_path"] == result.relative_path


def test_publish_never_overwrites_an_existing_user_file(tmp_path: Path) -> None:
    store, item_id, vault = prepared(tmp_path)
    expected = publish(store, item_id, vault)
    target = vault / expected.relative_path
    target.write_text("用户自己的内容", encoding="utf-8")
    with connect(store.path) as connection:
        connection.execute(
            """
            UPDATE knowledge_results SET published_path = NULL, published_at = NULL
            WHERE knowledge_result_id = 1
            """
        )

    conflict = publish(store, item_id, vault)

    assert conflict.state is PublicationState.CONFLICT
    assert target.read_text(encoding="utf-8") == "用户自己的内容"
    assert store.item_bundle(item_id)["published_path"] is None


def test_retry_recovers_exact_file_after_database_recording_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, item_id, vault = prepared(tmp_path)
    recorder = store.mark_published

    def fail_once(knowledge_result_id: int, relative_path: str, *, vault: Path) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "mark_published", fail_once)
    with pytest.raises(RuntimeError, match="database unavailable"):
        publish(store, item_id, vault)

    monkeypatch.setattr(store, "mark_published", recorder)
    recovered = publish(store, item_id, vault)

    assert recovered.state is PublicationState.RECOVERED
    assert store.item_bundle(item_id)["published_path"] == recovered.relative_path


def test_published_result_is_not_rewritten(tmp_path: Path) -> None:
    store, item_id, vault = prepared(tmp_path)
    first = publish(store, item_id, vault)

    second = publish(store, item_id, vault)

    assert second.state is PublicationState.ALREADY_PUBLISHED
    assert second.relative_path == first.relative_path


def test_vault_switch_cannot_redirect_an_existing_publication(tmp_path: Path, monkeypatch) -> None:
    from knowledge_distiller.v1.web import create_app
    from urllib.parse import quote

    store, item_id, original = prepared(tmp_path)
    published = publish(store, item_id, original)
    store.mark_succeeded(item_id)
    other = tmp_path / "other-vault"
    other.mkdir()
    unrelated = other / published.relative_path
    unrelated.parent.mkdir()
    unrelated.write_text("another user document", encoding="utf-8")
    store.set_setting("vault_path", str(other))

    import json
    registry = tmp_path / ('AppData/Roaming/obsidian/obsidian.json' if __import__('sys').platform=='win32' else 'Library/Application Support/obsidian/obsidian.json')
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'vaults': {'original-id': {'path': str(original)}, 'other-id': {'path': str(other)}}}))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('APPDATA',str(tmp_path/'AppData/Roaming'))
    page = create_app(store, object()).test_client().get("/")

    assert "vault=original-id&amp;file=" in page.text
    assert "vault=other-id&amp;file=" not in page.text
    assert unrelated.read_text(encoding="utf-8") == "another user document"


def test_publisher_never_follows_a_user_symlink(tmp_path: Path) -> None:
    store, item_id, vault = prepared(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "知识蒸馏器").symlink_to(outside, target_is_directory=True)

    result = publish(store, item_id, vault)

    assert result.state is PublicationState.CONFLICT
    assert list(outside.iterdir()) == []
    assert store.item_bundle(item_id)["published_path"] is None


@pytest.mark.parametrize("blocked", [None, "root-symlink", "note-symlink", "file"])
def test_text_only_publication_preserves_existing_user_attachments(tmp_path, monkeypatch, blocked):
    import hashlib
    import re
    from knowledge_distiller.v1.publisher import publication_path
    store, item_id, vault = prepared(tmp_path)
    content = b"retained image bytes"
    member = {"member_id": "image-1", "sha256": hashlib.sha256(content).hexdigest(),
              "mime_type": "image/png", "content": content}
    monkeypatch.setattr(store, "media_members", lambda _: [member])
    note = vault / publication_path(1, "注意力需要边界")
    root = note.parent / "附件"
    directory = root / note.stem
    outside = tmp_path / "outside"
    outside.mkdir()
    note.parent.mkdir()
    if blocked == "root-symlink":
        root.symlink_to(outside, target_is_directory=True)
    elif blocked == "note-symlink":
        root.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
    elif blocked == "file":
        directory.mkdir(parents=True)
        (directory / f"sf-1-image-1-{member['sha256']}.png").write_bytes(b"user image")
    result = publish(store, item_id, vault)
    assert result.state is PublicationState.PUBLISHED
    assert '![[附件/' not in note.read_text()
    assert not list(outside.iterdir())
    if blocked=='file':assert next(directory.iterdir()).read_bytes()==b'user image'
    if blocked=='root-symlink':assert root.is_symlink()
    if blocked=='note-symlink':assert directory.is_symlink()
    if blocked is None:assert not root.exists()
    with connect(store.path) as db:
        db.execute("UPDATE knowledge_results SET published_path=NULL, published_at=NULL")
    assert publish(store,item_id,vault).state is PublicationState.RECOVERED
