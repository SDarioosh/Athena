/*
 * Integration check of the phone-side upload path against a RUNNING convo server:
 *   CONVO_URL=http://127.0.0.1:8765 CONVO_TOKEN=... CHUNK=/path/chunk.opus \
 *     node --test tests/uploader.integration.mjs
 * Skipped unless CONVO_URL is set.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildEnvelope } from '../shared/envelope.js'
import { ping, uploadEnvelope, endSession } from '../shared/uploader.js'

const url = process.env.CONVO_URL
const cfg = { url, token: process.env.CONVO_TOKEN }

// Zepp Side Service fetch signature: one object, string body, {status, body}
async function zfetch({ url, method, headers, body }) {
  const r = await fetch(url, { method, headers, body })
  return { status: r.status, body: await r.text() }
}

test('phone → server upload path', { skip: !url }, async () => {
  assert.equal((await ping(zfetch, cfg)).ok, true)
  const audio = readFileSync(process.env.CHUNK)
  const session = `trex-${Date.now()}`
  const env = buildEnvelope({ device: 'trex3pro', session_id: session, sequence: 0,
    timestamp: new Date().toISOString(), codec: 'opus' }, new Uint8Array(audio))
  const r = await uploadEnvelope(zfetch, cfg, env)
  console.log(r)
  assert.equal(r.ok, true)
  assert.ok(r.speech > 0, 'server VAD should find speech in the sample')
  assert.equal((await uploadEnvelope(zfetch, cfg, env)).duplicate, true)
  assert.equal((await uploadEnvelope(zfetch, { ...cfg, token: 'wrong-token' }, env)).ok, false)
  assert.equal((await endSession(zfetch, cfg, session)).ok, true)
})
