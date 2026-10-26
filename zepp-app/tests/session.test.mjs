// Run: node --test tests/   (Node >= 22; the app modules are plain ES modules)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createTester, CHUNK_MS, chunkFileName, summarize } from '../shared/session.js'
import { buildEnvelope, parseEnvelope, base64 } from '../shared/envelope.js'

function harness({ failStart = false } = {}) {
  let t = 1_760_000_000_000
  const box = { size: 60_000 }
  const timers = []
  const started = []
  let stored
  const deps = {
    recorder: {
      start: (f) => {
        if (failStart) throw new Error('no mic')
        started.push(f)
      },
      stop: () => {},
    },
    fileSize: () => box.size,
    load: () => (stored ? JSON.parse(JSON.stringify(stored)) : undefined),
    save: (s) => {
      stored = JSON.parse(JSON.stringify(s))
    },
    now: () => t,
    setTimeout: (fn, ms) => {
      timers.push({ fn, at: t + ms })
      return timers.length
    },
    clearTimeout: (i) => {
      timers[i - 1] = null
    },
  }
  return {
    deps,
    box,
    started,
    advance(ms) {
      const end = t + ms
      for (;;) {
        const due = timers
          .map((x, i) => [x, i])
          .filter(([x]) => x && x.at <= end)
          .sort((a, b) => a[0].at - b[0].at)[0]
        if (!due) break
        t = due[0].at
        timers[due[1]] = null
        due[0].fn()
      }
      t = end
    },
    jump(ms) {
      t += ms // time passes without timers firing (page dead / JS suspended)
    },
    fireAllLate() {
      const pending = timers.filter(Boolean)
      timers.length = 0
      for (const x of pending) x.fn()
    },
  }
}

test('screen-on run rotates every 30 s and summarizes', () => {
  const h = harness()
  const tester = createTester(h.deps)
  tester.start('screen-on')
  h.advance(10 * 60 * 1000 + 5000)
  tester.stop()
  const s = tester.summary()
  assert.equal(s.chunks, 21)
  assert.equal(s.interrupted, 0)
  assert.equal(s.late, 0)
  assert.equal(s.recordedSec, 605)
  assert.equal(s.maxGapSec, 0)
  assert.equal(s.bytesPerSec, 2000)
  assert.equal(h.started[1], chunkFileName(tester.state.sessionId, 1))
})

test('page destroyed after screen-off is detected on relaunch', () => {
  const h = harness()
  let tester = createTester(h.deps)
  tester.start('screen-off')
  h.advance(15_000)
  tester.heartbeat()
  tester.pageDestroyed() // system exits the app ~10 s after screen-off
  h.jump(120_000) // user wakes the watch 2 min later; app relaunches
  tester = createTester(h.deps) // fresh page instance, state from storage
  assert.equal(tester.resumeIfInterrupted(), true)
  h.advance(CHUNK_MS)
  tester.stop()
  const s = tester.summary()
  assert.equal(s.relaunches, 1)
  assert.equal(s.interrupted, 1)
  assert.equal(s.maxGapSec, 120)
  assert.ok(tester.state.events.some((e) => e.msg.includes('relaunched while recording')))
})

test('suspended JS shows up as a late chunk', () => {
  const h = harness()
  const tester = createTester(h.deps)
  tester.start('screen-off')
  h.jump(95_000) // timer could not fire while JS was frozen
  h.fireAllLate()
  tester.stop()
  assert.equal(tester.state.chunks[0].note, 'late 65s')
  assert.equal(tester.summary().late, 1)
})

test('recorder failure stops cleanly and is logged', () => {
  const h = harness({ failStart: true })
  const tester = createTester(h.deps)
  tester.start('screen-on')
  assert.equal(tester.state.recording, false)
  assert.ok(tester.state.events.some((e) => e.msg.includes('recorder.start failed: no mic')))
  assert.equal(tester.state.lastError, 'recorder.start failed: no mic')
  // The next successful start clears the on-screen error.
  const ok = harness()
  const again = createTester(ok.deps)
  again.error('earlier problem')
  again.start('screen-on')
  assert.equal(again.state.recording, true)
  assert.equal(again.state.lastError, null)
  // An async start failure (e.g. START event or 2s status check) stops recording and rotation.
  again.error('recorder is IDLE 2 s after start')
  assert.equal(again.state.recording, false)
  assert.equal(again.state.lastError, 'recorder is IDLE 2 s after start')
  assert.equal(again.state.chunks.length, 0)
  ok.advance(CHUNK_MS * 2)
  assert.equal(ok.started.length, 1)
})

test('empty files are counted', () => {
  const h = harness()
  const tester = createTester(h.deps)
  tester.start('screen-on')
  h.box.size = 0
  h.advance(CHUNK_MS)
  tester.stop()
  assert.equal(tester.summary().empty, 2)
})

test('envelope round trip and base64 match Node', () => {
  const audio = new Uint8Array(1001).map((_, i) => (i * 37) % 256)
  const header = { device: 'trex3pro', session_id: 'trex-1', sequence: 3, timestamp: '2026-10-18T10:00:00.000Z' }
  const env = parseEnvelope(buildEnvelope(header, audio.buffer))
  assert.deepEqual(env.header, header)
  assert.deepEqual(Array.from(env.audio), Array.from(audio))
  for (const n of [0, 1, 2, 3, 1001]) {
    const b = audio.subarray(0, n)
    assert.equal(base64(b), Buffer.from(b).toString('base64'))
  }
  assert.throws(() => buildEnvelope({ name: 'Zoë' }, audio), /ASCII/)
  assert.throws(() => parseEnvelope(new Uint8Array([200, 0, 0, 0, 1])), /header length/)
})

test('an upload answer for an older session does not mark the new session', () => {
  const h = harness()
  const tester = createTester(h.deps)
  tester.start('screen-on')
  const old = tester.state.sessionId
  h.advance(CHUNK_MS)
  tester.stop()
  h.advance(1000)
  tester.start('screen-off') // started while the old session's upload was still running
  h.advance(CHUNK_MS)
  tester.stop()
  tester.markUploaded(0, true, old) // answer for the old session's chunk 0
  assert.equal(tester.state.chunks[0].uploaded, false)
  tester.markUploaded(0, true, tester.state.sessionId)
  assert.equal(tester.state.chunks[0].uploaded, true)
})

test('the chunk timer stops when the page is destroyed', () => {
  const h = harness()
  const tester = createTester(h.deps)
  tester.start('screen-off')
  h.advance(10_000)
  tester.pageDestroyed()
  h.advance(CHUNK_MS * 3) // a JS VM that outlives the page must not rotate chunks
  assert.equal(h.started.length, 1)
  assert.equal(tester.state.chunks.length, 0)
  assert.equal(tester.state.recording, true) // kept, so a relaunch sees the interruption
})

test('envelope accepts any ArrayBuffer view, with its offset', () => {
  const bytes = new Uint8Array([0, 0, 5, 6, 7, 8, 9, 0])
  const header = { device: 'trex3pro', sequence: 1 }
  const view = new DataView(bytes.buffer, 2, 5)
  const env = buildEnvelope(header, view)
  assert.deepEqual(Array.from(parseEnvelope(env).audio), [5, 6, 7, 8, 9])
  const back = parseEnvelope(new DataView(env))
  assert.deepEqual(back.header, header)
  assert.deepEqual(Array.from(back.audio), [5, 6, 7, 8, 9])
})

test('B/s stays finite when full chunks have no duration (clock jump)', () => {
  const s = summarize({
    recording: false, sessionStart: 1000, relaunches: 0,
    chunks: [{ seq: 0, start: 5000, end: 5000, size: 900, note: 'ok', uploaded: false }],
  }, 6000)
  assert.equal(s.bytesPerSec, 0)
})
