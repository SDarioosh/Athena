"""A recording that failed (e.g. the GPU was full during diarization) kept its audio;
``retry-failed`` sends it back to the worker, and it becomes a conversation."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from athena import cli
from athena.db.store import Store
from athena.diarize_pyannote import run_with_cpu_fallback
from tests.conftest import silence, tone, wav


def _ingest(pipeline, session="s1"):
    pipeline.ingest_chunk(session_id=session, device="omi", sequence=0,
                          started_at=1_700_000_000, codec="wav",
                          data=wav(np.concatenate([tone(200, 5), silence(25)])))


def test_a_failed_recording_becomes_a_conversation_after_a_retry(pipeline):
    pipeline.transcriber.fail = True
    _ingest(pipeline)
    pipeline.end_session("s1")
    pipeline.process_pending()
    assert pipeline.store.get_session("s1")["status"] == "failed"
    assert pipeline.store.list_conversations() == []

    pipeline.transcriber.fail = False
    assert pipeline.store.retry_failed_sessions() == ["s1"]
    sess = pipeline.store.get_session("s1")
    assert sess["status"] == "ended" and sess["error"] is None
    pipeline.process_pending()
    assert pipeline.store.get_session("s1")["status"] == "done"
    assert len(pipeline.store.list_conversations()) == 1
    assert pipeline.store.retry_failed_sessions() == []


def test_retry_failed_command_only_touches_the_database(settings, monkeypatch, capsys):
    store = Store(settings.db_path)
    store.add_chunk(session_id="s1", device="omi", sequence=0, started_at=1.0, duration=30.0,
                    codec="wav", audio_path=None, speech=[])
    store.set_session_status("s1", "failed", "OutOfMemoryError: CUDA out of memory")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_pipeline", lambda s: pytest.fail("loaded the models"))
    cli.main(["retry-failed"])
    assert "sent 1 failed recording(s) back to the worker: s1" in capsys.readouterr().out
    assert store.get_session("s1")["status"] == "ended"


class _OOM(Exception):
    pass


class _FakeTorch:
    emptied = 0
    cuda = SimpleNamespace(OutOfMemoryError=_OOM)

    @classmethod
    def _empty(cls):
        cls.emptied += 1

    @staticmethod
    def device(name):
        return name


_FakeTorch.cuda.empty_cache = _FakeTorch._empty


class _FakePipeline:
    def __init__(self):
        self.on = "cuda"
        self.moves = []

    def to(self, device):
        self.moves.append(device)
        self.on = device


def test_a_full_gpu_diarizes_on_the_cpu_and_goes_back_next_time():
    pipe = _FakePipeline()

    def run():
        if pipe.on == "cuda" and len(pipe.moves) < 2:
            raise _OOM("CUDA out of memory")
        return pipe.on

    assert run_with_cpu_fallback(_FakeTorch, pipe, "cuda", run) == "cpu"
    assert run_with_cpu_fallback(_FakeTorch, pipe, "cuda", run) == "cuda"


def test_cpu_device_runs_once_without_fallback():
    pipe = _FakePipeline()
    with pytest.raises(_OOM):
        run_with_cpu_fallback(_FakeTorch, pipe, "cpu", lambda: (_ for _ in ()).throw(_OOM()))
    assert pipe.moves == []


def test_the_gpu_cache_is_given_back_after_every_recording():
    pipe = _FakePipeline()
    before = _FakeTorch.emptied
    assert run_with_cpu_fallback(_FakeTorch, pipe, "cuda", lambda: "ok") == "ok"
    assert _FakeTorch.emptied == before + 1
    with pytest.raises(ValueError):
        run_with_cpu_fallback(_FakeTorch, pipe, "cuda",
                              lambda: (_ for _ in ()).throw(ValueError()))
    assert _FakeTorch.emptied == before + 2
