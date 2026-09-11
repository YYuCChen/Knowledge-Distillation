from __future__ import annotations

from urllib.parse import quote

import pytest

import knowledge_distiller.legacy.web as web_module
from knowledge_distiller.legacy.accepted_insight_publisher import (
    AcceptedPublicationKind,
    AcceptedPublicationResult,
    publish_accepted_insight,
)
from knowledge_distiller.accepted_insight_renderer import (
    accepted_insight_relative_path,
)
from knowledge_distiller.database import connect
from tests.test_accepted_insight_library import _accepted_first
from tests.test_accepted_insight_publisher import _setup_current
from tests.test_accepted_insight_renderer import _context
from tests.test_accepted_insight_web import _web_app
from tests.test_insight_judgment_service import (
    _produce_first_version,
    _produce_successor,
    _service_for,
)


SUCCESS_FEEDBACK = {
    AcceptedPublicationKind.PUBLISHED: "新文件与正式发布成功事实已建立",
    AcceptedPublicationKind.RECOVERED: "按落位收据恢复正式发布成功事实",
    AcceptedPublicationKind.ALREADY_PUBLISHED: (
        "此前已成功发布；本次没有检查、修复或改写用户文件"
    ),
}


def _publication_rows(database_path):
    with connect(database_path) as connection:
        return connection.execute(
            """
            SELECT * FROM accepted_insight_publications
            ORDER BY publication_id
            """
        ).fetchall()


@pytest.mark.parametrize(
    ("kind", "status", "feedback"),
    [
        (AcceptedPublicationKind.PUBLISHED, 303, SUCCESS_FEEDBACK[AcceptedPublicationKind.PUBLISHED]),
        (AcceptedPublicationKind.RECOVERED, 303, SUCCESS_FEEDBACK[AcceptedPublicationKind.RECOVERED]),
        (
            AcceptedPublicationKind.ALREADY_PUBLISHED,
            303,
            SUCCESS_FEEDBACK[AcceptedPublicationKind.ALREADY_PUBLISHED],
        ),
        (AcceptedPublicationKind.CONFLICT, 409, "没有覆盖已有目标"),
        (AcceptedPublicationKind.NOT_ELIGIBLE, 422, "这版新知不能发布"),
        (AcceptedPublicationKind.FAILED, 500, "这次发布没有完成"),
    ],
)
def test_publish_route_calls_exact_publisher_once_and_maps_all_results(
    tmp_path,
    monkeypatch,
    kind,
    status,
    feedback,
):
    path, vault, _, version_id = _setup_current(tmp_path, f"route-{kind.value}")
    if kind in SUCCESS_FEEDBACK:
        assert publish_accepted_insight(path, version_id, vault).kind is (
            AcceptedPublicationKind.PUBLISHED
        )
    calls = []

    def publish(database_path, exact_version_id, vault_root):
        calls.append((database_path, exact_version_id, vault_root))
        return AcceptedPublicationResult(exact_version_id, kind)

    monkeypatch.setattr(web_module, "publish_accepted_insight", publish)
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")

    assert response.status_code == status
    assert calls == [(path, version_id, vault)]
    if status == 303:
        assert response.headers["Location"].endswith(
            f"/knowledge/insights/{version_id}?publication_notice={kind.value}"
        )
        detail = client.get(response.headers["Location"])
        assert detail.status_code == 200
        assert feedback in detail.text
    else:
        assert feedback in response.text
        assert "A and B reveal a narrower boundary" not in response.text


@pytest.mark.parametrize("kind", tuple(SUCCESS_FEEDBACK))
def test_forged_success_notice_is_ignored_without_formal_publication(
    tmp_path,
    kind,
):
    path = tmp_path / f"forged-notice-{kind.value}.sqlite3"
    vault = tmp_path / f"forged-notice-{kind.value}-vault"
    vault.mkdir()
    _, version_id = _accepted_first(path)
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.get(
        f"/knowledge/insights/{version_id}?publication_notice={kind.value}"
    )

    assert response.status_code == 200
    assert SUCCESS_FEEDBACK[kind] not in response.text
    assert response.text.count(
        f'action="/knowledge/insights/{version_id}/publish"'
    ) == 1
    assert "这版 AI 衍生新知已由正式记忆记录为曾成功发布" not in response.text


@pytest.mark.parametrize("historical", [False, True])
def test_current_and_historical_publish_through_real_web_route(
    tmp_path,
    historical,
):
    path, vault, insight_id, version_id = _setup_current(
        tmp_path,
        f"web-happy-{historical}",
    )
    if historical:
        successor = _produce_successor(path, insight_id, version_id, "web-historical")
        assert _service_for(path).record_judgment(
            successor,
            "interesting",
        ).kind == "recorded"
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")
    detail = client.get(response.headers["Location"])

    assert response.status_code == 303
    assert "publication_notice=published" in response.headers["Location"]
    assert detail.status_code == 200
    assert SUCCESS_FEEDBACK[AcceptedPublicationKind.PUBLISHED] in detail.text
    assert "这版 AI 衍生新知已由正式记忆记录为曾成功发布" in detail.text
    assert "在 Obsidian 中打开这版新知" in detail.text
    assert f'action="/knowledge/insights/{version_id}/publish"' not in detail.text
    assert ("这是一份历史新知" in detail.text) is historical
    row = _publication_rows(path)[0]
    target = vault / str(row["relative_path"])
    assert target.is_file()
    assert len(_publication_rows(path)) == 1


def test_web_route_recovers_receipt_without_rewriting_target(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "web-recovery")

    def fail_after_insert(point):
        if point == "after_publication_insert":
            raise OSError("simulated database failure")

    failed = publish_accepted_insight(
        path,
        version_id,
        vault,
        failure_injector=fail_after_insert,
    )
    target = vault / failed.relative_path
    before = target.stat()
    before_bytes = target.read_bytes()
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")
    detail = client.get(response.headers["Location"])

    after = target.stat()
    assert response.status_code == 303
    assert "publication_notice=recovered" in response.headers["Location"]
    assert SUCCESS_FEEDBACK[AcceptedPublicationKind.RECOVERED] in detail.text
    assert target.read_bytes() == before_bytes
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )
    assert len(_publication_rows(path)) == 1


def test_already_published_feedback_does_not_check_or_repair_deleted_target(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "web-already")
    published = publish_accepted_insight(path, version_id, vault)
    target = vault / published.relative_path
    target.unlink()
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")
    detail = client.get(response.headers["Location"])

    assert response.status_code == 303
    assert "publication_notice=already_published" in response.headers["Location"]
    assert SUCCESS_FEEDBACK[AcceptedPublicationKind.ALREADY_PUBLISHED] in detail.text
    assert not target.exists()
    assert len(_publication_rows(path)) == 1


def test_conflict_is_409_and_keeps_user_target_and_database_unchanged(tmp_path):
    path, vault, insight_id, version_id = _setup_current(tmp_path, "web-conflict")
    target = vault / accepted_insight_relative_path(insight_id, 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# User-owned target\n", encoding="utf-8")
    before = target.read_bytes()
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")

    assert response.status_code == 409
    assert "没有覆盖已有目标" in response.text
    assert "A and B reveal a narrower boundary" not in response.text
    assert target.read_bytes() == before
    assert _publication_rows(path) == []


def test_runtime_source_failure_is_500_without_target_or_success(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "web-runtime-failure")
    context = _context(path, version_id)
    source = vault / context.source_leaves[0].published_path
    source.unlink()
    target = vault / accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")

    assert response.status_code == 500
    assert "这次发布没有完成" in response.text
    assert "A and B reveal a narrower boundary" not in response.text
    assert not target.exists()
    assert _publication_rows(path) == []


def test_unconfigured_vault_has_no_form_and_direct_post_is_honest_500(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("KNOWLEDGE_DISTILLER_OBSIDIAN_VAULT", raising=False)
    path = tmp_path / "web-unconfigured.sqlite3"
    _, version_id = _accepted_first(path)
    app = _web_app(path, vault_root=None)[0]
    detail = app.test_client().get(f"/knowledge/insights/{version_id}")

    calls = []

    def fail(database_path, exact_version_id, vault_root):
        calls.append((database_path, exact_version_id, vault_root))
        return AcceptedPublicationResult(
            exact_version_id,
            AcceptedPublicationKind.FAILED,
            error_code="accepted_vault_unavailable",
        )

    monkeypatch.setattr(web_module, "publish_accepted_insight", fail)
    response = app.test_client().post(
        f"/knowledge/insights/{version_id}/publish"
    )

    assert detail.status_code == 200
    assert "Obsidian Vault 尚未配置" in detail.text
    assert f'action="/knowledge/insights/{version_id}/publish"' not in detail.text
    assert response.status_code == 500
    assert "Vault 未配置或当前不可用" in response.text
    assert calls == [(path, version_id, None)]
    assert _publication_rows(path) == []


@pytest.mark.parametrize("state", ["pending", "rethink", "missing", "invalid"])
def test_direct_post_not_eligible_never_leaks_or_creates_assets(tmp_path, state):
    path = tmp_path / f"web-not-eligible-{state}.sqlite3"
    vault = tmp_path / f"web-not-eligible-{state}-vault"
    vault.mkdir()
    _, version_id = _produce_first_version(path)
    if state == "rethink":
        assert _service_for(path).record_judgment(
            version_id,
            "rethink",
        ).kind == "recorded"
    elif state == "missing":
        version_id = 999_999
    elif state == "invalid":
        version_id = 0
    client = _web_app(path, vault_root=vault)[0].test_client()

    response = client.post(f"/knowledge/insights/{version_id}/publish")

    assert response.status_code == 422
    assert "这版新知不能发布" in response.text
    assert "A and B reveal a narrower boundary" not in response.text
    assert _publication_rows(path) == []
    assert list(vault.rglob("*")) == []


class _NoVaultAccess:
    def __init__(self, path):
        self.path = path

    def __bool__(self):
        return True

    def __fspath__(self):
        return str(self.path)

    def resolve(self, *_args, **_kwargs):
        raise AssertionError("GET must not resolve or stat the Vault")

    def stat(self, *_args, **_kwargs):
        raise AssertionError("GET must not stat the Vault")

    def is_file(self, *_args, **_kwargs):
        raise AssertionError("GET must not inspect Vault files")

    def __truediv__(self, _other):
        raise AssertionError("GET must not traverse the Vault")


def test_published_detail_is_sqlite_only_after_target_deletion(tmp_path, monkeypatch):
    path, vault, _, version_id = _setup_current(tmp_path, "web-sqlite-only")
    published = publish_accepted_insight(path, version_id, vault)
    target = vault / published.relative_path
    target.unlink()
    publication_before = tuple(tuple(row) for row in _publication_rows(path))

    def explode(*_args, **_kwargs):
        raise AssertionError("GET must not call the accepted publisher")

    monkeypatch.setattr(web_module, "publish_accepted_insight", explode)
    guarded_vault = _NoVaultAccess(vault)
    app = _web_app(path, vault_root=guarded_vault)[0]
    client = app.test_client()
    witness = connect(path)
    try:
        before = int(witness.execute("PRAGMA data_version").fetchone()[0])
        listed = client.get("/knowledge/insights")
        detail = client.get(f"/knowledge/insights/{version_id}")
        after = int(witness.execute("PRAGMA data_version").fetchone()[0])
    finally:
        witness.close()

    expected_uri = "obsidian://open?path=" + quote(
        str(vault / published.relative_path),
        safe="",
    )
    assert listed.status_code == detail.status_code == 200
    assert "这版 AI 衍生新知已由正式记忆记录为曾成功发布" in detail.text
    assert expected_uri in detail.text
    assert f'action="/knowledge/insights/{version_id}/publish"' not in detail.text
    assert before == after
    assert tuple(tuple(row) for row in _publication_rows(path)) == publication_before
    assert not target.exists()
