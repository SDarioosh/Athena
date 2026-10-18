// Loops zml's real messaging code back to itself (Side Service build, no BLE), to check
// that an audio envelope reaches onRequest as binary and not as an empty JSON object.
import test from 'node:test'
import assert from 'node:assert/strict'
import { buildEnvelope, parseEnvelope } from '../shared/envelope.js'
import { UPLOAD_OPTIONS } from '../shared/messaging.js'

const quiet = { info() {}, warn() {}, error() {}, log() {} }
globalThis.Logger = { getLogger: () => quiet }
const listeners = []
globalThis.messaging = {
  peerSocket: {
    addListener: (_ev, fn) => listeners.push(fn),
    send: (buf) => setTimeout(() => listeners.forEach((fn) => fn(buf)), 0),
  },
}
globalThis.getApp = () => ({ port2: 1 })

async function loopback() {
  // The package's exports map hides this file, so import it by path.
  const { messagingPlugin } = await import(
    new URL('../node_modules/@zeppos/zml/dist/3.0/module/messaging/plugin/side.js', import.meta.url))
  const received = []
  const ctx = {
    ...messagingPlugin(),
    log() {}, error() {},
    onRequest(req, res) {
      received.push(req)
      res(null, { ok: true })
    },
  }
  ctx.onInit()
  return { ctx, received }
}

const { ctx, received } = await loopback()
const env = buildEnvelope({ device: 'trex3pro', session_id: 's1', sequence: 0 }, new Uint8Array(5000).fill(7))

test('without contentType bin, zml turns an ArrayBuffer into an empty JSON object', async () => {
  received.length = 0
  await ctx.request(env, { timeout: 2000, dataType: 'json' })
  assert.equal(received[0].byteLength, undefined)
  assert.equal(received[0].jsonrpc, 'hmrpcv1')
})

test('with UPLOAD_OPTIONS the Side Service gets the whole envelope as binary', async () => {
  received.length = 0
  const r = await ctx.request(env, { timeout: 2000, ...UPLOAD_OPTIONS })
  assert.deepEqual(r, { ok: true })
  assert.ok(received[0] instanceof ArrayBuffer)
  const back = parseEnvelope(received[0])
  assert.equal(back.header.session_id, 's1')
  assert.equal(back.audio.length, 5000)
})
