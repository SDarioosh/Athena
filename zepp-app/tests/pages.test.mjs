// Watch pages run in Node against fake Zepp OS modules (tests/fakes/). Covers page
// behaviour the pure state machines can't see: timers after onDestroy, buttons during
// an upload, upload messages, the Done button colour, and reading files.
import { test, beforeEach, afterEach, mock } from 'node:test'
import assert from 'node:assert/strict'
import { loadPage, fake } from './fakes/pages.mjs'
import { MIN_MS } from '../shared/enroll.js'

const T0 = 1_760_000_000_000
const flush = () => new Promise((r) => setImmediate(r))

beforeEach(() => {
  fake.reset()
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: T0 })
})
afterEach(() => mock.timers.reset())

function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

// ------------------------------------------------------------------ voice profile
test('enroll page draws nothing after onDestroy (Back while recording)', async () => {
  const page = await loadPage('enroll')
  page.onInit()
  page.build()
  page.start()
  mock.timers.tick(20)
  assert.equal(page.enroll.state.phase, 'recording')
  page.onDestroy()
  const before = fake.widgets.length
  mock.timers.tick(5000)
  assert.equal(fake.widgets.length, before, 'a widget was created on the destroyed page')
})

test('enroll page draws nothing when an upload answers after onDestroy', async () => {
  const answer = deferred()
  const page = await loadPage('enroll', () => answer.promise)
  page.onInit()
  page.build()
  page.start()
  mock.timers.tick(MIN_MS + 1000)
  page.finish()
  mock.timers.tick(20)
  fake.files.set(page.enroll.state.file, new Uint8Array([1, 2, 3]))
  page.upload()
  mock.timers.tick(20)
  assert.equal(page.enroll.state.phase, 'uploading')
  page.onDestroy()
  const before = fake.widgets.length
  answer.resolve({ ok: true, name: 'Me', speech: 50 })
  await flush()
  mock.timers.tick(5000)
  assert.equal(fake.widgets.length, before)
})

test('Done turns red even when the 1 s tick is late around the minimum', async () => {
  const page = await loadPage('enroll')
  page.onInit()
  page.start()
  mock.timers.tick(20)
  const red = 0xc0392b
  assert.notEqual(fake.button('Done').props.normal_color, red)
  // ticks at 44.97 s and 46.02 s: the second one falls outside |elapsed - MIN_MS| < 1 s
  mock.timers.setTime(page.enroll.state.startedAt + MIN_MS - 30)
  page.updateInfo()
  mock.timers.tick(20)
  assert.notEqual(fake.button('Done').props.normal_color, red)
  mock.timers.setTime(page.enroll.state.startedAt + MIN_MS + 1020)
  page.updateInfo()
  mock.timers.tick(20)
  assert.equal(fake.button('Done').props.normal_color, red)
})

test('Again on the last prompt starts the prompts over', async () => {
  const page = await loadPage('enroll')
  page.onInit()
  page.start()
  mock.timers.tick(20)
  for (let i = 0; i < 7; i++) page.next()
  mock.timers.tick(20)
  assert.ok(fake.button('Again'))
  fake.button('Again').props.click_func()
  mock.timers.tick(20)
  assert.equal(page.enroll.state.prompt, 0)
  assert.ok(fake.button('Next'))
})

// ------------------------------------------------------------------ recording test
function seedChunks(n) {
  const chunks = []
  for (let i = 0; i < n; i++) {
    const file = `c_000001_${String(i).padStart(4, '0')}.opus`
    fake.files.set(file, new Uint8Array([i + 1, 2, 3]))
    chunks.push({ seq: i, file, start: T0 - 100_000 + i * 30_000,
                  end: T0 - 70_000 + i * 30_000, size: 3, note: 'ok', uploaded: false })
  }
  fake.storage.set('state', JSON.stringify({
    recording: false, mode: 'screen-on', sessionId: 'trex-1', seq: n - 1,
    sessionStart: T0 - 100_000, chunks, events: [],
  }))
  return chunks
}

test('Clear, Screen ON/OFF and Voice do nothing while an upload runs', async () => {
  const chunks = seedChunks(2)
  const answers = []
  const page = await loadPage('index', () => {
    const d = deferred()
    answers.push(d)
    return d.promise
  })
  page.onInit()
  page.build()
  page.uploadAll()
  assert.equal(answers.length, 1)
  for (const name of ['Clear', 'Screen ON', 'Screen OFF', 'Voice']) {
    const b = fake.button(name)
    assert.ok(b, `${name} button missing: the page left the idle screen`)
    b.props.click_func()
    mock.timers.tick(20)
  }
  assert.equal(page.tester.state.recording, false, 'a new test started mid-upload')
  assert.equal(page.tester.state.sessionId, 'trex-1')
  assert.equal(page.tester.state.chunks.length, 2, 'chunks were cleared mid-upload')
  for (const c of chunks) assert.ok(fake.files.has(c.file), `${c.file} was deleted`)
  assert.equal(fake.pushed.length, 0)
  answers[0].resolve({ ok: true })
  await flush()
  answers[1].resolve({ ok: true })
  await flush()
  assert.equal(page.tester.summary().uploaded, 2)
  assert.equal(page.state.uploading, false)
})

test('a server stop answer is still shown when the upload finishes', async () => {
  seedChunks(2)
  const page = await loadPage('index', async (req) => {
    if (req && req.method === 'end') return { ok: true }
    return { ok: true, stop: true, reason: 'silence' }
  })
  page.onInit()
  page.build()
  page.uploadAll()
  for (let i = 0; i < 6; i++) await flush()
  assert.equal(page.state.uploading, false)
  assert.match(page.state.net, /upload done/)
  assert.match(page.state.net, /server says stop \(silence\)/)
})

// ------------------------------------------------------------------ files
test('readFile closes the file and refuses bad handles or short reads', async () => {
  await loadPage('index') // registers the fake modules
  const { readFile } = await import('../utils/device.js')
  fake.files.set('a.opus', new Uint8Array([9, 8, 7, 6]))
  assert.deepEqual(Array.from(new Uint8Array(readFile('a.opus'))), [9, 8, 7, 6])
  assert.equal(fake.fds.size, 0)

  fake.openResult = -2 // a failed open must not upload a zero-filled buffer
  assert.throws(() => readFile('a.opus'), /open/)
  fake.openResult = null

  fake.readResult = 2 // short read: the rest of the buffer would be zeros
  assert.throws(() => readFile('a.opus'), /read 2 of 4/)
  assert.equal(fake.fds.size, 0, 'fd leaked after a short read')
  fake.readResult = null

  fake.readThrows = new Error('io')
  assert.throws(() => readFile('a.opus'), /io/)
  assert.equal(fake.fds.size, 0, 'fd leaked after readSync threw')
})
