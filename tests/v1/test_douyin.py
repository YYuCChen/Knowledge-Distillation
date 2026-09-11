from pathlib import Path

import pytest

import knowledge_distiller.v1.douyin as douyin
from knowledge_distiller.media import MediaVerificationError


class Session:
    def cookies(self):
        return {"sessionid": "private"}


def test_native_browser_completeness_error_reaches_item_without_generic_relabel(tmp_path, monkeypatch):
    from knowledge_distiller.v1.douyin_collection_browser import BrowserCollectionClient
    from knowledge_distiller.v1.douyin_collections import CollectionError
    async def incomplete(self):
        raise CollectionError('douyin_gallery_incomplete')
    monkeypatch.setattr(BrowserCollectionClient, '__aenter__', incomplete)
    binding = douyin.InstalledDouyinBinding({'sessionid': 'fixture'}, session=Session())
    with pytest.raises(douyin.DouyinSourceError, match='douyin_gallery_incomplete'):
        binding.download('https://www.douyin.com/note/123', tmp_path)


class Binding:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, Path]] = []

    def download(self, submitted_url: str, work_dir: Path):
        self.calls.append((submitted_url, work_dir))
        if self.error is not None:
            raise self.error
        return self.result


class Verifier:
    def __init__(self, duration: float = 12.5, error: Exception | None = None):
        self.duration = duration
        self.error = error
        self.calls = []

    def verify(self, path: Path, **kwargs) -> float:
        self.calls.append((path, kwargs, path.read_bytes()))
        if self.error is not None:
            raise self.error
        return self.duration


def download(tmp_path: Path) -> douyin.DouyinDownload:
    media = tmp_path / "upstream" / "123.mp4"
    media.parent.mkdir()
    media.write_bytes(b"complete media")
    return douyin.DouyinDownload(
        item_id="123",
        canonical_url="https://www.douyin.com/video/123",
        media_path=media,
        expected_duration_seconds=12.4,
        author_name="测试作者",
        author_id="author-1",
        description="原始描述",
        published_at="2026-09-01T00:00:00+00:00",
    )


def test_source_returns_one_verified_domain_material(tmp_path: Path) -> None:
    binding = Binding(download(tmp_path))
    verifier = Verifier()
    source = douyin.DouyinSource(
        Session(),
        binding_factory=lambda cookies: binding,
        verifier=verifier,
    )

    captured = source.capture(
        "https://v.douyin.com/example/", tmp_path / "work"
    )

    assert captured.source_kind == "douyin"
    assert captured.source_key == "123"
    assert captured.canonical_url == "https://www.douyin.com/video/123"
    assert captured.media_path.read_bytes() == b"complete media"
    assert captured.duration_seconds == 12.5
    from datetime import datetime
    assert datetime.fromisoformat(captured.metadata["captured_at"]).tzinfo is not None
    assert {k:v for k,v in captured.metadata.items() if k != "captured_at"} == {
        "author": {
            "display_name": "测试作者",
            "platform_account_id": "author-1",
        },
        "original_description": "原始描述",
        "published_at": "2026-09-01T00:00:00+00:00",
    }
    assert verifier.calls[0][1] == {
        "expected_duration_seconds": 12.4,
        "complete_decode": True,
    }


def test_source_reverifies_retained_media_without_upstream_access(
    tmp_path: Path,
) -> None:
    verifier = Verifier()
    source = douyin.DouyinSource(Session(), verifier=verifier)
    work_dir = tmp_path / "work"
    media = work_dir / "media" / "source.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"retained media")

    captured = source.reuse_retained(
        source_key="123",
        submitted_url="https://v.douyin.com/example/",
        canonical_url="https://www.douyin.com/video/123",
        metadata={"original_description": "原始描述"},
        work_dir=work_dir,
    )

    assert captured is not None
    assert captured.source_key == "123"
    assert captured.media_path == media
    assert verifier.calls == [
        (
            media,
            {"expected_duration_seconds": None, "complete_decode": True},
            b"retained media",
        )
    ]


def test_invalid_retained_media_is_removed_before_retry(
    tmp_path: Path,
) -> None:
    source = douyin.DouyinSource(
        Session(),
        verifier=Verifier(error=MediaVerificationError("broken")),
    )
    work_dir = tmp_path / "work"
    media = work_dir / "media" / "source.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"broken retained media")

    with pytest.raises(douyin.DouyinSourceError) as failure:
        source.reuse_retained(
            source_key="123",
            submitted_url="https://v.douyin.com/example/",
            canonical_url="https://www.douyin.com/video/123",
            metadata={},
            work_dir=work_dir,
        )

    assert failure.value.args == ("douyin_media_invalid",)
    assert not media.exists()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (douyin._LoginRequired(), "douyin_login_required"),
        (douyin._Unsupported(), "douyin_input_unsupported"),
        (douyin._SourceUnavailable(), "douyin_source_unavailable"),
        (douyin._UpstreamFailed(), "douyin_upstream_failed"),
    ],
)
def test_upstream_failure_is_translated_once(
    tmp_path: Path, error: Exception, code: str
) -> None:
    source = douyin.DouyinSource(
        Session(),
        binding_factory=lambda cookies: Binding(error=error),
        verifier=Verifier(),
    )

    with pytest.raises(douyin.DouyinSourceError) as failure:
        source.capture("https://v.douyin.com/example/", tmp_path / "work")

    assert failure.value.args == (code,)


def test_invalid_media_is_removed_and_never_becomes_captured(
    tmp_path: Path,
) -> None:
    source = douyin.DouyinSource(
        Session(),
        binding_factory=lambda cookies: Binding(download(tmp_path)),
        verifier=Verifier(error=MediaVerificationError("broken")),
    )

    with pytest.raises(douyin.DouyinSourceError) as failure:
        source.capture("https://v.douyin.com/example/", tmp_path / "work")

    assert failure.value.args == ("douyin_media_invalid",)
    assert not (tmp_path / "work" / "media" / "source.mp4").exists()


def test_native_article_capture_does_not_enter_video_path(tmp_path):
    from PIL import Image
    from knowledge_distiller.v1.douyin_text import DouyinText, DouyinTextImage
    picture=tmp_path/'image.png';Image.new('RGB',(32,32),'white').save(picture)
    native=DouyinText('article','123','https://www.douyin.com/article/123','标题','正文〔图片 1〕',
        (DouyinTextImage('image-1','id','https://p3.douyinpic.com/image.png'),),'version','正文')
    result=douyin.DouyinTextDownload(native,(picture,),{'published_at':None})
    captured=douyin.DouyinSource(Session(),binding_factory=lambda _:Binding(result)).capture(native.canonical_url,tmp_path/'work')
    assert captured.source_kind=='douyin' and captured.metadata['note_kind']=='normal'
    assert captured.metadata['original_description']=='正文〔图片 1〕'
    assert len(captured.members)==1 and captured.members[0].mime_type=='image/png'


def test_live_photo_capture_records_static_scope_and_no_motion_member(tmp_path):
    from PIL import Image
    from knowledge_distiller.v1.douyin_text import parse_douyin_text
    picture = tmp_path / 'image.png'
    Image.new('RGB', (32, 32), 'white').save(picture)
    native = parse_douyin_text({'aweme_type': 0, 'aweme_id': '123', 'desc': '配文',
        'images': [{'uri': 'original', 'url_list': ['https://p3.douyinpic.com/image.png'],
                    'video': {'duration': 2033}}]})
    result = douyin.DouyinTextDownload(native, (picture,), {})
    captured = douyin.DouyinSource(Session(), binding_factory=lambda _: Binding(result)).capture(
        native.canonical_url, tmp_path / 'work')
    assert captured.metadata['motion_omitted'] == ['image-1']
    assert '动态部分及其声音未处理' in captured.metadata['source_scope']
    assert captured.metadata['original_description'] == '配文'
    assert len(captured.members) == 1
    assert captured.members[0].mime_type == 'image/png'
