/*
 * Side Service networking, separated from AppSideService so it can be tested with Node
 * against a real server. `zfetch` has the Zepp Side Service fetch signature:
 * fetch({url, method, headers, body}) -> {status, body}.
 */
import { parseEnvelope, base64 } from './envelope.js'

function parseBody(res) {
  try {
    return typeof res.body === 'string' ? JSON.parse(res.body) : res.body
  } catch (e) {
    return { raw: String(res.body).slice(0, 200) }
  }
}

const errText = (e) => String(e && e.message ? e.message : e)

export async function ping(zfetch, { url }) {
  if (!url) return { ok: false, error: 'set server URL in app settings' }
  const t0 = Date.now()
  try {
    const res = await zfetch({ url: `${url}/health`, method: 'GET' })
    return { ok: res.status === 200, status: res.status, ms: Date.now() - t0 }
  } catch (e) {
    return { ok: false, error: errText(e) }
  }
}

export async function uploadEnvelope(zfetch, { url, token }, buf) {
  if (!url || !token) return { ok: false, error: 'set server URL and token in app settings' }
  let env
  try {
    env = parseEnvelope(buf)
  } catch (e) {
    return { ok: false, error: `bad envelope: ${e.message}` }
  }
  try {
    const res = await zfetch({
      url: `${url}/v1/ingest/chunk.json`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ ...env.header, audio_b64: base64(env.audio) }),
    })
    const data = parseBody(res) || {}
    if (res.status !== 200) return { ok: false, error: `HTTP ${res.status} ${data.detail || ''}` }
    return { ok: true, stop: !!data.stop, reason: data.reason || null, duplicate: !!data.duplicate,
             speech: data.speech_seconds, bytes: env.audio.length }
  } catch (e) {
    return { ok: false, error: errText(e) }
  }
}

export async function endSession(zfetch, { url, token }, sessionId) {
  if (!url || !token || !sessionId) return { ok: false }
  try {
    const res = await zfetch({
      url: `${url}/v1/sessions/${encodeURIComponent(sessionId)}/end`,
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
    return { ok: res.status === 200 }
  } catch (e) {
    return { ok: false, error: errText(e) }
  }
}
