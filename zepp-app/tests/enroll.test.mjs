// Run: node --test tests/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createEnrollment, PROMPTS, MIN_MS, MAX_MS, enrollFileName } from '../shared/enroll.js'
import { buildEnvelope } from '../shared/envelope.js'
import { uploadEnvelope } from '../shared/uploader.js'

function harness({ failStart = false } = {}) {
  let t = 1_760_000_000_000
  const timers = []
  const calls = []
  const e = createEnrollment({
    recorder: {
      start: (f) => {
        if (failStart) throw new Error('no mic')
        calls.push(['start', f])
      },
      stop: () => calls.push(['stop']),
    },
    now: () => t,
    setTimeout: (fn, ms) => timers.push({ fn, at: t + ms }) ,
    clearTimeout: (i) => { timers[i - 1] = null },
  })
  return {
    e, calls,
    advance(ms) {
      t += ms
      for (const tm of timers) if (tm && tm.at <= t) { tm.at = Infinity; tm.fn() }
    },
  }
}

test('guided recording: prompts, Done only after the minimum, then upload', () => {
  const h = harness()
  h.e.start()
  assert.equal(h.e.state.phase, 'recording')
  assert.deepEqual(h.calls[0], ['start', enrollFileName(1_760_000_000_000)])
  assert.equal(h.e.promptText(), PROMPTS[0])
  h.e.next()
  assert.equal(h.e.promptText(), PROMPTS[1])
  for (let i = 0; i < PROMPTS.length - 2; i++) h.e.next()
  assert.equal(h.e.state.prompt, PROMPTS.length - 1)
  h.e.next() // "Again" on the last prompt starts over
  assert.equal(h.e.state.prompt, 0)

  h.advance(MIN_MS - 1000)
  assert.equal(h.e.finish(), false) // too short: still recording
  assert.equal(h.e.state.phase, 'recording')
  h.advance(30_000)
  assert.equal(h.e.finish(), true)
  assert.equal(h.e.state.phase, 'recorded')
  assert.deepEqual(h.calls.at(-1), ['stop'])
  assert.deepEqual(h.e.uploadHeader(),
    { kind: 'enroll', is_owner: true, codec: 'opus', duration_ms: MIN_MS + 29_000 })

  assert.equal(h.e.uploadStarted(), true)
  h.e.uploadFinished({ ok: false, error: 'HTTP 503 diarization is not enabled' })
  assert.equal(h.e.state.phase, 'recorded') // kept for a retry
  assert.match(h.e.state.error, /503/)
  h.e.uploadStarted()
  h.e.uploadFinished({ ok: true, name: 'Me', speech: 61.5 })
  assert.equal(h.e.state.phase, 'done')
})

test('recording stops by itself at the maximum length', () => {
  const h = harness()
  h.e.start()
  h.advance(MAX_MS)
  assert.equal(h.e.state.phase, 'recorded')
  assert.equal(h.e.elapsedMs(), MAX_MS)
})

test('cancel and recorder errors', () => {
  const h = harness()
  h.e.start()
  h.e.cancel()
  assert.equal(h.e.state.phase, 'idle')
  h.advance(MAX_MS) // the auto-stop timer was cleared
  assert.equal(h.e.state.phase, 'idle')
  const bad = harness({ failStart: true })
  bad.e.start()
  assert.equal(bad.e.state.phase, 'error')
  assert.match(bad.e.state.error, /no mic/)
})

test('the phone posts a voice profile as the owner', async () => {
  const sent = []
  const zfetch = async (req) => {
    sent.push(req)
    return { status: 200, body: JSON.stringify({ ok: true, name: 'Sam', is_owner: true,
                                                 speech_seconds: 64.2 }) }
  }
  const env = buildEnvelope({ kind: 'enroll', is_owner: true, codec: 'opus', duration_ms: 70000 },
    new Uint8Array([1, 2, 3]))
  const r = await uploadEnvelope(zfetch, { url: 'http://s', token: 't', ownerName: ' Sam ' }, env)
  assert.deepEqual(r, { ok: true, name: 'Sam', speech: 64.2, bytes: 3 })
  assert.equal(sent[0].url, 'http://s/v1/speakers/enroll.json')
  assert.equal(sent[0].headers.Authorization, 'Bearer t')
  assert.deepEqual(JSON.parse(sent[0].body),
    { name: 'Sam', is_owner: true, codec: 'opus', audio_b64: 'AQID' })

  const noName = await uploadEnvelope(zfetch, { url: 'http://s', token: 't' }, env)
  assert.equal(noName.ok, true)
  assert.equal(JSON.parse(sent[1].body).name, 'Me')

  const failing = async () => ({ status: 503, body: '{"detail":"diarization is not enabled"}' })
  assert.deepEqual(await uploadEnvelope(failing, { url: 'http://s', token: 't' }, env),
    { ok: false, error: 'HTTP 503 diarization is not enabled' })
})
