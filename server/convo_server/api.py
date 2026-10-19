"""HTTP API.

Device ingestion:
    POST /v1/ingest/chunk          multipart audio chunk -> {"ok", "stop", ...}
    POST /v1/sessions/{id}/end     device pressed Stop
Read API (used by the knowledge-layer/MCP integration and for debugging):
    GET  /v1/sessions, /v1/sessions/{id}
    GET  /v1/conversations, /v1/conversations/{id}, /v1/conversations/{id}/transcript
    GET  /v1/search?q=...
    GET  /v1/action-items, PATCH /v1/action-items/{id}
    GET  /v1/digests/{date}, POST /v1/digests/{date}
    POST /v1/speakers/enroll
Every /v1 route requires ``Authorization: Bearer <CONVO_INGEST_TOKEN>``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import numpy as np
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import audio as audio_mod
from .config import Settings
from .diarize import enroll_voice, load_voices
from .links import render_conversation, transcript_link, verify
from .llm import LLMError
from .pipeline import Pipeline, Worker
from .search import search

log = logging.getLogger(__name__)
MAX_CHUNK_BYTES = 25 * 1024 * 1024


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None


def parse_ts(value: str) -> float:
    """ISO-8601 (naive = UTC) or epoch seconds/milliseconds."""
    try:
        num = float(value)
        return num / 1000.0 if num > 1e11 else num
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, "timestamp must be ISO-8601 or epoch seconds") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _conv_out(c: dict[str, Any], settings: Settings) -> dict[str, Any]:
    out = {k: v for k, v in c.items() if k not in ("started_at", "ended_at", "updated_at")}
    out["started_at"] = iso(c["started_at"])
    out["ended_at"] = iso(c["ended_at"])
    out["transcript_url"] = f"/v1/conversations/{c['id']}/transcript"
    out["link"] = transcript_link(settings, c["id"])
    for key in ("action_items", "facts"):
        for item in out.get(key, []):
            src = item.get("source_at")
            item["source_at"] = iso(src)
            if src is not None:
                item["source_url"] = transcript_link(settings, c["id"], src - c["started_at"])
    return out


class ChunkJson(BaseModel):
    device: str
    session_id: str
    sequence: int
    timestamp: str
    codec: str = "opus"
    sample_rate: int = audio_mod.SAMPLE_RATE
    audio_b64: str = Field(max_length=MAX_CHUNK_BYTES * 4 // 3 + 4)


class ActionItemPatch(BaseModel):
    status: str


REJECTED_KEEP = 20  # undecodable uploads kept for debugging, newest first


def save_rejected(settings: Settings, session_id: str, sequence: int, codec: str,
                  data: bytes) -> str:
    """Keep an upload ffmpeg could not decode, so its format can be inspected.

    The file stays in the server's data directory, like any queued audio. Only the newest
    REJECTED_KEEP files are kept.
    """
    folder = settings.data_dir / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{session_id}-{sequence}.{codec}")[:120]
    path = folder / f"{safe}.bin"
    path.write_bytes(data)
    old = sorted(folder.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in old[REJECTED_KEEP:]:
        stale.unlink(missing_ok=True)
    return str(path)


def create_app(pipeline: Pipeline, *, start_worker: bool | None = None) -> FastAPI:
    settings = pipeline.settings
    store = pipeline.store
    token = settings.ingest_token.encode()

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), token):
            raise HTTPException(401, "invalid or missing bearer token",
                                headers={"WWW-Authenticate": "Bearer"})

    run_worker = settings.run_worker if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = Worker(pipeline) if run_worker else None
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="convo-ai server", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/view/{conv_id}", response_class=HTMLResponse, include_in_schema=False)
    def view_conversation(conv_id: str, sig: str = "") -> HTMLResponse:
        """Human-readable transcript for signed links (no bearer token needed)."""
        conv = store.get_conversation(conv_id) if verify(settings, conv_id, sig) else None
        if conv is None:  # same answer for bad signature and unknown id
            raise HTTPException(404, "not found")
        page = render_conversation(settings, conv, store.conversation_segments(conv_id))
        return HTMLResponse(page, headers={"Cache-Control": "no-store",
                                           "Referrer-Policy": "no-referrer"})

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

    # ---------------------------------------------------------------- ingest
    @v1.post("/ingest/chunk")
    async def ingest_chunk(
        audio: Annotated[bytes, File()],
        device: Annotated[str, Form()],
        session_id: Annotated[str, Form()],
        sequence: Annotated[int, Form()],
        timestamp: Annotated[str, Form()],
        codec: Annotated[str, Form()] = "opus",
        sample_rate: Annotated[int, Form()] = audio_mod.SAMPLE_RATE,
    ) -> dict[str, Any]:
        if len(audio) > MAX_CHUNK_BYTES:
            raise HTTPException(413, "chunk too large")
        started = parse_ts(timestamp)
        try:
            result = await run_in_threadpool(
                pipeline.ingest_chunk, device=device, session_id=session_id, sequence=sequence,
                started_at=started, codec=codec, data=audio, sample_rate=sample_rate)
        except audio_mod.AudioDecodeError as e:
            saved = save_rejected(settings, session_id, sequence, codec, audio)
            log.warning("could not decode %d bytes (%s), saved to %s: %s",
                        len(audio), codec, saved, e)
            raise HTTPException(422, f"could not decode audio: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {**result.as_dict(), "session_id": session_id, "sequence": sequence}

    @v1.post("/ingest/chunk.json")
    async def ingest_chunk_json(body: ChunkJson) -> dict[str, Any]:
        """Same as /ingest/chunk with base64 audio in JSON, for clients that can only send
        string bodies (the Zepp OS Side Service's fetch)."""
        try:
            audio = base64.b64decode(body.audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, "audio_b64 is not valid base64") from e
        return await ingest_chunk(audio=audio, device=body.device, session_id=body.session_id,
                                  sequence=body.sequence, timestamp=body.timestamp,
                                  codec=body.codec, sample_rate=body.sample_rate)

    @v1.post("/sessions/{session_id}/end")
    def end_session(session_id: str) -> dict[str, Any]:
        if not pipeline.end_session(session_id, "client"):
            raise HTTPException(404, "session not found")
        return {"ok": True, "stop": True}

    @v1.get("/sessions")
    def list_sessions(limit: int = 50) -> dict[str, Any]:
        rows = store.list_sessions(limit)
        for r in rows:
            for k in ("started_at", "last_chunk_at", "last_speech_at", "received_at"):
                r[k] = iso(r[k])
        return {"sessions": rows}

    @v1.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        s = store.get_session(session_id)
        if s is None:
            raise HTTPException(404, "session not found")
        chunks = store.chunks(session_id)
        return {**{k: iso(v) if k.endswith("_at") else v for k, v in s.items()},
                "chunks": [{"sequence": c["sequence"], "started_at": iso(c["started_at"]),
                            "duration": c["duration"], "speech": c["speech"],
                            "has_audio": c["audio_path"] is not None} for c in chunks]}

    # --------------------------------------------------------- conversations
    @v1.get("/conversations")
    def list_conversations(since: str | None = None, until: str | None = None,
                           person: str | None = None, limit: int = 50) -> dict[str, Any]:
        rows = store.list_conversations(
            since=parse_ts(since) if since else None, until=parse_ts(until) if until else None,
            person=person, limit=limit)
        return {"conversations": [_conv_out(r, settings) for r in rows]}

    @v1.get("/conversations/{conv_id}")
    def get_conversation(conv_id: str) -> dict[str, Any]:
        c = store.get_conversation(conv_id)
        if c is None:
            raise HTTPException(404, "conversation not found")
        return _conv_out(c, settings)

    @v1.get("/conversations/{conv_id}/transcript")
    def get_transcript(conv_id: str, start: float | None = None,
                       end: float | None = None) -> dict[str, Any]:
        """Transcript; ``start``/``end`` are offsets in seconds from conversation start."""
        c = store.get_conversation(conv_id)
        if c is None:
            raise HTTPException(404, "conversation not found")
        origin = c["started_at"]
        segs = []
        for s in store.conversation_segments(conv_id):
            off = s.started_at - origin
            if (start is not None and off < start) or (end is not None and off > end):
                continue
            segs.append({"offset": round(off, 1), "at": iso(s.started_at),
                         "speaker": s.speaker, "is_owner": s.is_owner, "text": s.text})
        return {"conversation_id": conv_id, "title": c["title"],
                "started_at": iso(origin), "segments": segs}

    @v1.post("/conversations/{conv_id}/summarize")
    async def resummarize(conv_id: str) -> dict[str, Any]:
        if store.get_conversation(conv_id) is None:
            raise HTTPException(404, "conversation not found")
        ok = await run_in_threadpool(pipeline.summarize_conversation, conv_id)
        return {"ok": ok}

    @v1.get("/search")
    async def search_route(q: Annotated[str, Query(min_length=1)],
                           limit: int = 10) -> dict[str, Any]:
        hits = await run_in_threadpool(search, store, q, pipeline.embedder, limit)
        for h in hits:
            h["started_at"], h["ended_at"] = iso(h["started_at"]), iso(h["ended_at"])
            h["transcript_url"] = f"/v1/conversations/{h['conversation_id']}/transcript"
        return {"query": q, "results": hits}

    # ---------------------------------------------------------- action items
    @v1.get("/action-items")
    def action_items(status: str | None = "open") -> dict[str, Any]:
        rows = store.list_action_items(None if status == "all" else status)
        for r in rows:
            r["source_at"], r["conversation_at"] = iso(r["source_at"]), iso(r["conversation_at"])
        return {"action_items": rows}

    @v1.patch("/action-items/{item_id}")
    def patch_action_item(item_id: int, body: ActionItemPatch) -> dict[str, Any]:
        if body.status not in {"open", "done", "dismissed"}:
            raise HTTPException(400, "status must be open, done or dismissed")
        if not store.set_action_item_status(item_id, body.status):
            raise HTTPException(404, "action item not found")
        return {"ok": True}

    # --------------------------------------------------------------- digests
    @v1.get("/digests/{date}")
    def get_digest(date: str) -> dict[str, Any]:
        d = store.get_digest(date)
        if d is None:
            raise HTTPException(404, "no digest for that date")
        return d

    @v1.post("/digests/{date}")
    async def make_digest(date: str) -> dict[str, Any]:
        try:
            pipeline.day_bounds(date)
        except ValueError as e:
            raise HTTPException(400, "date must be YYYY-MM-DD") from e
        try:
            d = await run_in_threadpool(pipeline.make_digest, date)
        except LLMError as e:
            raise HTTPException(502, str(e)) from e
        if d is None:
            raise HTTPException(404, "no summarized conversations that day")
        return d

    # -------------------------------------------------------------- speakers
    @v1.post("/speakers/enroll")
    async def enroll(audio: Annotated[bytes, File()], name: Annotated[str, Form()],
                     is_owner: Annotated[bool, Form()] = False,
                     codec: Annotated[str, Form()] = "wav") -> dict[str, Any]:
        diarizer = pipeline.diarizer
        if diarizer is None:
            raise HTTPException(503, "diarization is not enabled")
        try:
            pcm = audio_mod.decode(audio, codec)
        except audio_mod.AudioDecodeError as e:
            raise HTTPException(422, f"could not decode audio: {e}") from e
        speech = pipeline.vad.detect(pcm)
        if speech:
            sr = audio_mod.SAMPLE_RATE
            pcm = np.concatenate([pcm[int(s.start * sr):int(s.end * sr)]
                                            for s in speech])
        try:
            await run_in_threadpool(enroll_voice, store, diarizer.embedder, name, pcm,
                                    is_owner)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        diarizer.voices = load_voices(store)
        return {"ok": True, "name": name, "is_owner": is_owner,
                "speech_seconds": round(audio_mod.duration_seconds(pcm), 1)}

    @v1.get("/speakers")
    def speakers() -> dict[str, Any]:
        return {"speakers": [{"name": s["name"], "is_owner": s["is_owner"]}
                             for s in store.speakers()]}

    app.include_router(v1)
    return app

