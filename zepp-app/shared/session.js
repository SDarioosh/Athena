/*
 * Recording-test state machine, independent of Zepp OS APIs so it can be unit-tested
 * with Node (see tests/session.test.mjs). The page injects the real recorder, file
 * system, storage and timers.
 *
 * What it measures: does recording keep going in 30 s chunks while the screen is on /
 * off? Every chunk is logged with wall-clock start/end and file size. When the page is
 * destroyed mid-recording (e.g. the system exits the app ~10 s after screen-off) and
 * later relaunched, the gap and the interrupted chunk are recorded too.
 */

export const CHUNK_MS = 30000
export const BEAT_MS = 5000
const MAX_EVENTS = 60

export function emptyState() {
  return {
    recording: false,
    mode: null, // 'screen-on' | 'screen-off'
    sessionId: null,
    seq: 0,
    sessionStart: 0,
    chunkStart: 0,
    chunkFile: null,
    lastBeat: 0,
    relaunches: 0,
    lastError: null, // shown on screen; cleared by the next start()
    chunks: [], // {seq, file, start, end, size, note, uploaded}
    events: [], // {t, msg}
  }
}

export function chunkFileName(sessionId, seq) {
  return `c_${sessionId.slice(-6)}_${String(seq).padStart(4, '0')}.opus`
}

/**
 * @param {object} deps
 *   recorder: { start(file), stop() }          wraps @zos/media Recorder
 *   fileSize(file) -> number|undefined          wraps @zos/fs statSync
 *   load() -> state|undefined, save(state)      wraps @zos/storage LocalStorage
 *   now() -> ms, setTimeout, clearTimeout
 *   onChange(state)                             UI refresh hook
 */
export function createTester(deps) {
  const { recorder, fileSize, load, save, now, onChange = () => {} } = deps
  let state = Object.assign(emptyState(), load() || {})
  let rotateTimer = null

  function log(msg) {
    state.events.push({ t: now(), msg })
    if (state.events.length > MAX_EVENTS) state.events.splice(0, state.events.length - MAX_EVENTS)
  }

  function fail(msg) {
    state.lastError = msg
    log(msg)
  }

  function persist() {
    save(state)
    onChange(state)
  }

  function startChunk() {
    const file = chunkFileName(state.sessionId, state.seq)
    try {
      recorder.start(file)
    } catch (e) {
      fail(`recorder.start failed: ${e && e.message ? e.message : e}`)
      state.recording = false
      persist()
      return
    }
    state.chunkFile = file
    state.chunkStart = now()
    state.lastBeat = state.chunkStart
    persist()
    rotateTimer = deps.setTimeout(rotate, CHUNK_MS)
  }

  function finishChunk(note, end) {
    if (!state.chunkFile) return
    const file = state.chunkFile
    state.chunks.push({
      seq: state.seq, file, start: state.chunkStart, end: end || now(),
      size: fileSize(file) || 0, note, uploaded: false,
    })
    state.chunkFile = null
  }

  function rotate() {
    rotateTimer = null
    if (!state.recording) return
    const late = now() - state.chunkStart - CHUNK_MS
    try {
      recorder.stop()
    } catch (e) {
      log(`recorder.stop failed: ${e}`)
    }
    // A timer that fires much later than 30 s means JS was suspended (screen off).
    finishChunk(late > 3000 ? `late ${Math.round(late / 1000)}s` : 'ok')
    state.seq += 1
    startChunk()
  }

  return {
    get state() {
      return state
    },

    /** Call from the page's onInit: detects a page that died while recording. */
    resumeIfInterrupted() {
      if (!state.recording) return false
      const gap = now() - (state.lastBeat || state.chunkStart)
      state.relaunches += 1
      finishChunk('interrupted', state.lastBeat || state.chunkStart)
      log(`relaunched while recording; ~${Math.round(gap / 1000)}s since last heartbeat`)
      state.seq += 1
      startChunk()
      return true
    },

    start(mode) {
      if (state.recording) return
      const t = now()
      state = Object.assign(emptyState(), {
        recording: true, mode, sessionId: `trex-${t}`, seq: 0, sessionStart: t,
      })
      log(`start (${mode})`)
      startChunk()
    },

    stop(reason = 'stop') {
      if (!state.recording) return
      if (rotateTimer) deps.clearTimeout(rotateTimer)
      rotateTimer = null
      try {
        recorder.stop()
      } catch (e) {
        log(`recorder.stop failed: ${e}`)
      }
      finishChunk(reason)
      state.recording = false
      log(`stop (${reason}); ${state.chunks.length} chunks`)
      persist()
    },

    /** Called every BEAT_MS by the page; the last beat bounds when the page died. */
    heartbeat() {
      if (!state.recording) return
      state.lastBeat = now()
      save(state)
    },

    pageDestroyed() {
      if (state.recording) {
        log('page destroyed while recording')
        state.lastBeat = now()
        save(state)
      }
    },

    markUploaded(seq, result) {
      const c = state.chunks.find((x) => x.seq === seq)
      if (c) c.uploaded = result
      persist()
    },

    log(msg) {
      log(msg)
      persist()
    },

    /** Like log(), but also shown on screen until the next start(). */
    error(msg) {
      fail(msg)
      persist()
    },

    clear() {
      if (state.recording) return
      state = emptyState()
      persist()
    },

    summary() {
      return summarize(state, now())
    },
  }
}

export function summarize(state, nowMs) {
  const chunks = state.chunks
  const recordedMs = chunks.reduce((a, c) => a + (c.end - c.start), 0)
  const wallEnd = state.recording ? nowMs : chunks.length ? chunks[chunks.length - 1].end : nowMs
  const wallMs = state.sessionStart ? wallEnd - state.sessionStart : 0
  let maxGap = 0
  for (let i = 1; i < chunks.length; i++) {
    maxGap = Math.max(maxGap, chunks[i].start - chunks[i - 1].end)
  }
  const full = chunks.filter((c) => c.note === 'ok' && c.size > 0)
  const bytesPerSec = full.length
    ? full.reduce((a, c) => a + c.size, 0) / (full.reduce((a, c) => a + (c.end - c.start), 0) / 1000)
    : 0
  return {
    chunks: chunks.length,
    empty: chunks.filter((c) => !c.size).length,
    interrupted: chunks.filter((c) => c.note === 'interrupted').length,
    late: chunks.filter((c) => String(c.note).startsWith('late')).length,
    relaunches: state.relaunches,
    recordedSec: Math.round(recordedMs / 1000),
    wallSec: Math.round(wallMs / 1000),
    maxGapSec: Math.round(maxGap / 100) / 10,
    bytesPerSec: Math.round(bytesPerSec),
    uploaded: chunks.filter((c) => c.uploaded === true).length,
  }
}
