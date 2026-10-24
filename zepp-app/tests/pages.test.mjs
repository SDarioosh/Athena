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
