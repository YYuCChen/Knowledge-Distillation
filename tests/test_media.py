import json
import subprocess

import pytest

from knowledge_distiller.media import (
    CommandResult,
    FFmpegMediaVerifier,
    MediaVerificationError,
)


class MediaToolRunner:
    def __init__(
        self,
        *,
        streams=None,
        duration=62.02,
        probe_returncode=0,
        video_returncode=0,
        audio_returncode=0,
    ):
        self.streams = streams or [
            {"codec_type": "video"},
            {"codec_type": "audio"},
        ]
        self.duration = duration
        self.probe_returncode = probe_returncode
        self.video_returncode = video_returncode
        self.audio_returncode = audio_returncode
        self.commands = []

    def run(self, command):
        self.commands.append(list(command))
        if command[0] == "ffprobe":
            return CommandResult(
                self.probe_returncode,
                json.dumps(
                    {
                        "streams": self.streams,
                        "format": {"duration": str(self.duration)},
                    }
                ),
                "",
            )
        stream = command[command.index("-map") + 1]
        return CommandResult(
            self.video_returncode if stream == "0:v:0" else self.audio_returncode,
            "",
            "",
        )


def make_media(tmp_path, content=b"media"):
    path = tmp_path / "source.mp4"
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("exists,content", [(False, b"media"), (True, b"")])
def test_missing_or_empty_media_fails_before_probe(tmp_path, exists, content):
    runner = MediaToolRunner()
    path = tmp_path / "source.mp4"
    if exists:
        path.write_bytes(content)

    with pytest.raises(MediaVerificationError):
        FFmpegMediaVerifier(runner).verify(
            path,
            expected_duration_seconds=None,
            complete_decode=True,
        )

    assert runner.commands == []


def test_unparseable_container_fails(tmp_path):
    runner = MediaToolRunner(probe_returncode=1)

    with pytest.raises(MediaVerificationError):
        FFmpegMediaVerifier(runner).verify(
            make_media(tmp_path),
            expected_duration_seconds=None,
            complete_decode=False,
        )


@pytest.mark.parametrize(
    "streams",
    [
        [{"codec_type": "audio"}],
        [{"codec_type": "video"}],
        ["not-a-stream"],
    ],
)
def test_media_requires_video_and_audio_streams(tmp_path, streams):
    runner = MediaToolRunner(streams=streams)

    with pytest.raises(MediaVerificationError):
        FFmpegMediaVerifier(runner).verify(
            make_media(tmp_path),
            expected_duration_seconds=None,
            complete_decode=False,
        )


@pytest.mark.parametrize(
    "actual,expected",
    [(0.0, None), (40.0, 62.0)],
)
def test_unreasonable_or_conflicting_duration_fails(tmp_path, actual, expected):
    runner = MediaToolRunner(duration=actual)

    with pytest.raises(MediaVerificationError):
        FFmpegMediaVerifier(runner).verify(
            make_media(tmp_path),
            expected_duration_seconds=expected,
            complete_decode=False,
        )


@pytest.mark.parametrize(
    "video_returncode,audio_returncode",
    [(1, 0), (0, 1)],
)
def test_new_media_requires_complete_decode_of_both_streams(
    tmp_path,
    video_returncode,
    audio_returncode,
):
    runner = MediaToolRunner(
        video_returncode=video_returncode,
        audio_returncode=audio_returncode,
    )

    with pytest.raises(MediaVerificationError):
        FFmpegMediaVerifier(runner).verify(
            make_media(tmp_path),
            expected_duration_seconds=62.0,
            complete_decode=True,
        )


def test_reused_media_rechecks_probe_without_full_decode(tmp_path):
    runner = MediaToolRunner()

    duration = FFmpegMediaVerifier(runner).verify(
        make_media(tmp_path),
        expected_duration_seconds=62.0,
        complete_decode=False,
    )

    assert duration == 62.02
    assert [command[0] for command in runner.commands] == ["ffprobe"]


def test_real_ffmpeg_tools_accept_a_complete_local_audio_video_fixture(tmp_path):
    path = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=10:duration=0.3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=8000:duration=0.3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )

    duration = FFmpegMediaVerifier().verify(
        path,
        expected_duration_seconds=0.3,
        complete_decode=True,
    )

    assert 0.2 <= duration <= 0.5
