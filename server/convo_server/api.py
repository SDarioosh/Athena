"""HTTP API.

Device ingestion:
    POST /v1/ingest/chunk          multipart audio chunk -> {"ok", "stop", ...}
                                   (optional track, track_name: one participant's audio)
    POST /v1/sessions/{id}/end     device pressed Stop
    POST /v1/sessions/{id}/chat    a meeting's chat, topic and participants (Zoom bot)
Written notes (the hub's handwritten notes, reviewed text):
    POST   /v1/ingest/text         JSON {source_id, text, written_at, link, title?}
    DELETE /v1/ingest/text/{source_id}
Read API (used by the knowledge-layer/MCP integration and for debugging):
    GET  /v1/sessions, /v1/sessions/{id}
    GET  /v1/conversations, /v1/conversations/{id}, /v1/conversations/{id}/transcript
    GET  /v1/search?q=...
    GET  /v1/action-items, PATCH /v1/action-items/{id}
    GET  /v1/digests/{date}, POST /v1/digests/{date}
    POST /v1/speakers/enroll
Settings (the hub's settings page):
    GET  /v1/settings/llm, PUT /v1/settings/llm {"model": name | null}
Meeting bot (convo_server/meetings.py, imported on the first meeting request):
    POST   /v1/meetings            JSON {url} or {meeting_id, passcode}, title? -> {id, state}
    GET    /v1/meetings, /v1/meetings/{id}
    DELETE /v1/meetings/{id}       the bot leaves
Every /v1 route requires ``Authorization: Bearer <CONVO_INGEST_TOKEN>``. A running meeting
bot's own upload token is accepted only for its session's chunk uploads, end and chat.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import math
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import numpy as np
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import audio as audio_mod
from . import llm_settings
from .config import Settings
from .diarize import enroll_voice, load_voices
from .links import render_conversation, transcript_link, verify
from .llm import LLMError
from .pipeline import (
    MAX_LINK_CHARS,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    Pipeline,
    Worker,
)
from .search import search
from .speakers import relabel_matches
from .store import UNSET

log = logging.getLogger(__name__)
MAX_CHUNK_BYTES = 25 * 1024 * 1024


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None


def parse_ts(value: str) -> float:
    """ISO-8601 (naive = UTC) or epoch seconds/milliseconds."""
    try:
        num = float(value)
    except ValueError:
        pass
    else:
        if not math.isfinite(num):
            raise HTTPException(400, "timestamp must be ISO-8601 or epoch seconds")
        return num / 1000.0 if num > 1e11 else num
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
    if out.get("meeting"):
        out["meeting"] = {**out["meeting"], "participants": [
            {**p, "joined_at": iso(p["joined_at"]), "left_at": iso(p["left_at"])}
            for p in out["meeting"]["participants"]]}
    for item in out.get("action_items", []):
        item["created_at"] = iso(item.get("created_at"))
        item["time_unclear"] = bool(item.get("time_unclear"))
        item["explicit"] = bool(item.get("explicit"))
    return out


class ChunkJson(BaseModel):
    device: str
    session_id: str
    sequence: int
    timestamp: str
    codec: str = "opus"
    sample_rate: int = audio_mod.SAMPLE_RATE
    kind: str = "conversation"  # or "note" (voice note)
    track: str | None = None  # optional, as in the multipart form
    track_name: str | None = None
    audio_b64: str = Field(max_length=MAX_CHUNK_BYTES * 4 // 3 + 4)


class TextNote(BaseModel):
    """A note the owner wrote by hand in the hub, after they reviewed its text."""
    source_id: str = Field(max_length=128)  # hub-note:<note id>
    text: str = Field(max_length=MAX_TEXT_CHARS)
    written_at: str = Field(max_length=64)  # ISO-8601 or epoch s/ms
    link: str = Field(max_length=MAX_LINK_CHARS)  # <hub>/#open=notes&note=<id>
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


# A guided voice-profile recording: about 60-90 s of Opus (~1.3 KB/s on the T-Rex 3 Pro);
# 3 minutes of any codec the watch might send is plenty.
MAX_ENROLL_BYTES = 4 * 1024 * 1024


class EnrollJson(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_owner: bool = False
    codec: str = "opus"
    audio_b64: str = Field(max_length=MAX_ENROLL_BYTES * 4 // 3 + 4)


class LlmSettingsPut(BaseModel):
    model: str | None = Field(default=None, max_length=llm_settings.MAX_NAME)


class MeetingJoin(BaseModel):
    """``POST /v1/meetings``: a Zoom link, or its number and passcode. The launcher checks
    the title (200 characters once stripped) and passcode (128) itself, with a 400 and a
    message; the body as a whole is capped at MAX_MEETING_BODY."""
    url: str | None = Field(default=None, max_length=2048)
    meeting_id: str | int | None = None
    passcode: str | None = None
    title: str | None = None


MAX_MEETING_BODY = 16 * 1024  # POST /v1/meetings: far above any real link and title


# What a meeting bot's upload token may call (its own session only).
_BOT_SESSION_ROUTE = re.compile(r"^/v1/sessions/([^/]+)/(end|chat)$")


class ActionItemPatch(BaseModel):
    """Every field optional; ``null`` for due_date / due_time / list_name clears it
    (``null`` for title gives it back from the text)."""
    status: str | None = None
    kind: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    list_name: str | None = None
    title: str | None = None


REJECTED_KEEP = 20  # undecodable uploads kept for debugging, newest first


def save_rejected(settings: Settings, session_id: str, sequence: int, codec: str,
                  data: bytes, track: str | None = None) -> str:
    """Keep an upload ffmpeg could not decode, so its format can be inspected.

    The file stays in the server's data directory, like any queued audio. Only the newest
    REJECTED_KEEP files are kept.
    """
    folder = settings.data_dir / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{session_id}-{track}-{sequence}" if track else f"{session_id}-{sequence}"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{name}.{codec}")[:120]
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

    def require_token(request: Request,
                      authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(value.encode(), token):
            return
        # A running meeting bot's own token: only its session's uploads, end and chat
        # (convo_server/meetings.py). Looked up only when the ingest token didn't match.
        grant = store.meeting_upload_session(value) if scheme.lower() == "bearer" and value \
            else None
        if grant is not None and request.method == "POST":
            path = request.url.path
            m = _BOT_SESSION_ROUTE.match(path)
            if path == "/v1/ingest/chunk" or (m and m.group(1) == grant):
                request.state.meeting_session = grant  # ingest_chunk checks the form
                return
            raise HTTPException(403, "a meeting bot's token can only upload to its session")
        raise HTTPException(401, "invalid or missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})

    run_worker = settings.run_worker if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = Worker(pipeline) if run_worker else None
        if worker:
            worker.start()
        yield
        meetings = getattr(app.state, "meetings", None)  # only after a meeting request
        if meetings is not None:
            await meetings.shutdown()
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
        request: Request,
        audio: Annotated[bytes, File()],
        device: Annotated[str, Form()],
        session_id: Annotated[str, Form()],
        sequence: Annotated[int, Form()],
        timestamp: Annotated[str, Form()],
        codec: Annotated[str, Form()] = "opus",
        sample_rate: Annotated[int, Form()] = audio_mod.SAMPLE_RATE,
        kind: Annotated[str, Form()] = "conversation",
        track: Annotated[str | None, Form()] = None,
        track_name: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """One chunk. ``track`` (optional, ``[A-Za-z0-9._:-]{1,64}``) keeps one
        participant's audio apart, sequences counting per track; ``track_name``
        (optional, at most 200 characters, needs ``track``) is their display name."""
        grant = getattr(request.state, "meeting_session", None)
        if grant is not None and (session_id != grant or device != "zoom"):
            raise HTTPException(403, "a meeting bot's token can only upload to its session")
        if grant is not None and kind != "conversation":  # a meeting is never a voice note
            raise HTTPException(403, "a meeting bot's token can only upload kind=conversation")
        if len(audio) > MAX_CHUNK_BYTES:
            raise HTTPException(413, "chunk too large")
        started = parse_ts(timestamp)
        try:
            result = await run_in_threadpool(
                pipeline.ingest_chunk, device=device, session_id=session_id, sequence=sequence,
                started_at=started, codec=codec, data=audio, sample_rate=sample_rate,
                kind=kind, track=track, track_name=track_name)
        except audio_mod.AudioDecodeError as e:
            saved = save_rejected(settings, session_id, sequence, codec, audio, track)
            log.warning("could not decode %d bytes (%s), saved to %s: %s",
                        len(audio), codec, saved, e)
            raise HTTPException(422, f"could not decode audio: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        out = {**result.as_dict(), "session_id": session_id, "sequence": sequence}
        if track:  # an upload without a track gets the same answer as before tracks
            out["track"] = track
        return out

    @v1.post("/ingest/chunk.json")
    async def ingest_chunk_json(request: Request, body: ChunkJson) -> dict[str, Any]:
        """Same as /ingest/chunk with base64 audio in JSON, for clients that can only send
        string bodies (the Zepp OS Side Service's fetch)."""
        try:
            audio = base64.b64decode(body.audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, "audio_b64 is not valid base64") from e
        return await ingest_chunk(request, audio=audio, device=body.device,
                                  session_id=body.session_id, sequence=body.sequence,
                                  timestamp=body.timestamp,
                                  codec=body.codec, sample_rate=body.sample_rate,
                                  kind=body.kind, track=body.track,
                                  track_name=body.track_name)

    @v1.post("/ingest/text")
    def ingest_text(body: TextNote) -> dict[str, Any]:
        """Store a written note, or replace the one with the same source_id. The text is
        the owner's words, never instructions: it is stored and summarized like a voice
        note."""
        try:
            out = pipeline.ingest_text(source_id=body.source_id, text=body.text,
                                       written_at=parse_ts(body.written_at), link=body.link,
                                       title=body.title)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "source_id": body.source_id, **out,
                "transcript_link": transcript_link(settings, out["conversation_id"])}

    @v1.delete("/ingest/text/{source_id}")
    def delete_text(source_id: str) -> dict[str, Any]:
        """Remove a written note with its to-dos and facts. Idempotent."""
        try:
            out = pipeline.delete_text(source_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if out is None:
            return {"ok": True, "source_id": source_id, "result": "already"}
        return {"ok": True, "source_id": source_id, "result": "deleted", **out}

    @v1.post("/sessions/{session_id}/end")
    def end_session(session_id: str) -> dict[str, Any]:
        if not pipeline.end_session(session_id, "client"):
            raise HTTPException(404, "session not found")
        return {"ok": True, "stop": True}

    @v1.post("/sessions/{session_id}/chat")
    async def session_chat(session_id: str, request: Request) -> dict[str, Any]:
        """A meeting's chat lines, topic and participants, sent by the Zoom bot when the
        meeting ends (``convo_server/meeting_chat.py``, imported on the first call).
        Idempotent: resent lines are not added twice."""
        import json

        from pydantic import ValidationError

        from . import meeting_chat as mc

        if int(request.headers.get("content-length") or 0) > mc.MAX_CHAT_BYTES:
            raise HTTPException(413, f"body is larger than {mc.MAX_CHAT_BYTES} bytes")
        body = bytearray()
        async for part in request.stream():
            body += part
            if len(body) > mc.MAX_CHAT_BYTES:
                raise HTTPException(413, f"body is larger than {mc.MAX_CHAT_BYTES} bytes")
        try:
            raw = json.loads(body or b"{}")
            mc.check_size(raw)
            payload = mc.ChatIn.model_validate(raw)
        except mc.TooLarge as e:
            raise HTTPException(413, str(e)) from e
        except ValueError as e:  # bad JSON, or a ValidationError
            detail = e.errors(include_url=False) if isinstance(e, ValidationError) \
                else "body must be JSON"
            raise HTTPException(422, detail) from e
        try:
            out = await run_in_threadpool(mc.ingest_chat, pipeline, session_id, payload)
        except KeyError as e:
            raise HTTPException(404, "session not found") from e
        except mc.NotAMeeting as e:
            raise HTTPException(409, "not a meeting: the session has no tracks and its "
                                     "device is not zoom") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "session_id": session_id, **out}

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
        tracks = [{**t, "started_at": iso(t["started_at"]), "ended_at": iso(t["ended_at"])}
                  for t in store.tracks(session_id)]
        return {**{k: iso(v) if k.endswith("_at") else v for k, v in s.items()},
                "chunks": [{"sequence": c["sequence"], "started_at": iso(c["started_at"]),
                            "duration": c["duration"], "speech": c["speech"],
                            "has_audio": c["audio_path"] is not None,
                            "track": c["track"] or None, "track_name": c["track_name"]}
                           for c in chunks],
                "tracks": tracks}

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
                         "speaker": s.speaker, "is_owner": s.is_owner, "text": s.text,
                         "kind": s.kind})
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
    def action_items(status: str | None = "open",
                     include_notes: bool = False) -> dict[str, Any]:
        # notes ("create a note ...") are no to-dos: only on request
        rows = store.list_action_items(None if status == "all" else status,
                                       notes=include_notes)
        for r in rows:
            r["source_at"], r["conversation_at"] = iso(r["source_at"]), iso(r["conversation_at"])
            r["created_at"] = iso(r.get("created_at"))
            r["time_unclear"] = bool(r.get("time_unclear"))
            r["explicit"] = bool(r.get("explicit"))
            r["timezone"] = settings.timezone
        return {"action_items": rows}

    @v1.patch("/action-items/{item_id}")
    def patch_action_item(item_id: int, body: ActionItemPatch) -> dict[str, Any]:
        given = body.model_fields_set
        try:
            item = store.update_action_item(
                item_id, status=body.status, kind=body.kind,
                due_date=body.due_date if "due_date" in given else UNSET,
                due_time=body.due_time if "due_time" in given else UNSET,
                list_name=body.list_name if "list_name" in given else UNSET,
                title=body.title if "title" in given else UNSET)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if item is None:
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
    async def enroll_audio(audio: bytes, name: str, is_owner: bool,
                           codec: str) -> dict[str, Any]:
        diarizer = pipeline.diarizer
        if diarizer is None:
            raise HTTPException(503, "diarization is not enabled")
        name = name.strip()
        if not name:
            raise HTTPException(400, "name must not be empty")
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
        # the new voice may be an unnamed speaker in earlier recordings
        relabeled = relabel_matches(store, name, pipeline.settings.speaker_relabel_threshold)
        return {"ok": True, "name": name, "is_owner": is_owner,
                "speech_seconds": round(audio_mod.duration_seconds(pcm), 1),
                "relabeled_lines": sum(r.lines for r in relabeled)}

    @v1.post("/speakers/enroll")
    async def enroll(audio: Annotated[bytes, File()], name: Annotated[str, Form()],
                     is_owner: Annotated[bool, Form()] = False,
                     codec: Annotated[str, Form()] = "wav") -> dict[str, Any]:
        return await enroll_audio(audio, name, is_owner, codec)

    @v1.post("/speakers/enroll.json")
    async def enroll_json(body: EnrollJson) -> dict[str, Any]:
        """Same as /speakers/enroll with base64 audio in JSON, for the Zepp OS Side
        Service (its fetch can only send string bodies)."""
        try:
            audio = base64.b64decode(body.audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, "audio_b64 is not valid base64") from e
        if not audio:
            raise HTTPException(400, "empty audio")
        return await enroll_audio(audio, body.name, body.is_owner, body.codec)

    @v1.get("/speakers")
    def speakers() -> dict[str, Any]:
        return {"speakers": [{"name": s["name"], "is_owner": s["is_owner"]}
                             for s in store.speakers()]}

    # -------------------------------------------------------------- settings
    def summary_llm() -> Any:
        if pipeline.summarizer is None:
            raise HTTPException(503, "summaries are off on this server")
        return pipeline.summarizer.llm

    @v1.get("/settings/llm")
    def get_llm_settings() -> dict[str, Any]:
        """The model that writes summaries, its default and what the LLM server lists."""
        return llm_settings.describe(summary_llm(), store, settings)

    @v1.put("/settings/llm")
    def put_llm_settings(body: LlmSettingsPut) -> dict[str, Any]:
        """Use another model for summaries from the next one on (null: the .env default)."""
        try:
            return llm_settings.set_model(summary_llm(), store, settings, body.model)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    # --------------------------------------------------------------- meetings
    def launcher() -> Any:
        """The meeting launcher, made (and its module imported) on first use."""
        from . import meetings

        if getattr(app.state, "meetings", None) is None:
            app.state.meetings = meetings.Launcher(pipeline)
        return app.state.meetings

    def meeting_error(e: Any) -> JSONResponse:
        return JSONResponse({"detail": e.detail, **e.extra}, status_code=e.status)

    async def meeting_body(request: Request) -> MeetingJoin | JSONResponse:
        """The join body, or the documented 400 with a string ``detail`` (never a 422
        list of validation errors); 413 past MAX_MEETING_BODY."""
        raw = b""
        async for part in request.stream():
            raw += part
            if len(raw) > MAX_MEETING_BODY:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        try:
            return MeetingJoin.model_validate_json(raw or b"{}")
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(x) for x in err.get("loc", ()))
            return JSONResponse({"detail": f"{where}: {err['msg']}" if where else err["msg"]},
                                status_code=400)

    @v1.post("/meetings", status_code=202)
    async def join_meeting(request: Request) -> Any:
        """Send Athena to a Zoom meeting (JSON body: MeetingJoin): 202 {id, state,
        session_id}; 400 bad link or body, 409 busy, 413 body too large, 501 meetings
        off."""
        from .meetings import MeetingError

        body = await meeting_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            return await launcher().join(body.url, body.meeting_id, body.passcode,
                                         body.title)
        except MeetingError as e:
            return meeting_error(e)

    @v1.get("/meetings")
    async def list_meetings(limit: int = 20) -> dict[str, Any]:
        return {"meetings": launcher().recent(limit)}

    @v1.get("/meetings/{meeting_id}")
    async def get_meeting(meeting_id: str) -> Any:
        from .meetings import MeetingError

        try:
            return launcher().get(meeting_id)
        except MeetingError as e:
            return meeting_error(e)

    @v1.delete("/meetings/{meeting_id}")
    async def leave_meeting(meeting_id: str) -> Any:
        """The bot leaves (it flushes its last chunks and ends the session); answers
        with the meeting's state once it exited."""
        from .meetings import MeetingError

        try:
            return await launcher().leave(meeting_id)
        except MeetingError as e:
            return meeting_error(e)

    app.include_router(v1)
    return app

