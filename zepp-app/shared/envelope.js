/*
 * Binary envelope for sending one audio chunk from the watch to the Side Service over
 * BLE messaging: [u32 LE header length][ASCII JSON header][audio bytes].
 * Pure JS (no Buffer/TextEncoder) so it works on the watch, the phone and in Node tests.
 */

function asciiBytes(str) {
  const out = new Uint8Array(str.length)
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i)
    if (c > 127) throw new Error('envelope header must be ASCII')
    out[i] = c
  }
  return out
}

function asciiString(bytes) {
  let s = ''
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i])
  return s
}

/** @returns {ArrayBuffer} */
export function buildEnvelope(header, audio) {
  const head = asciiBytes(JSON.stringify(header))
  const body = audio instanceof Uint8Array ? audio : new Uint8Array(audio)
  const out = new Uint8Array(4 + head.length + body.length)
  const n = head.length
  out[0] = n & 0xff
  out[1] = (n >>> 8) & 0xff
  out[2] = (n >>> 16) & 0xff
  out[3] = (n >>> 24) & 0xff
  out.set(head, 4)
  out.set(body, 4 + n)
  return out.buffer
}

/** @returns {{header: object, audio: Uint8Array}} */
export function parseEnvelope(buf) {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  if (bytes.length < 4) throw new Error('envelope too short')
  const n = bytes[0] | (bytes[1] << 8) | (bytes[2] << 16) | (bytes[3] << 24)
  if (n <= 0 || 4 + n > bytes.length) throw new Error('bad envelope header length')
  const header = JSON.parse(asciiString(bytes.subarray(4, 4 + n)))
  return { header, audio: bytes.subarray(4 + n) }
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

export function base64(bytes) {
  let out = ''
  let i = 0
  for (; i + 2 < bytes.length; i += 3) {
    const v = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2]
    out += B64[(v >> 18) & 63] + B64[(v >> 12) & 63] + B64[(v >> 6) & 63] + B64[v & 63]
  }
  const rest = bytes.length - i
  if (rest === 1) {
    const v = bytes[i] << 16
    out += B64[(v >> 18) & 63] + B64[(v >> 12) & 63] + '=='
  } else if (rest === 2) {
    const v = (bytes[i] << 16) | (bytes[i + 1] << 8)
    out += B64[(v >> 18) & 63] + B64[(v >> 12) & 63] + B64[(v >> 6) & 63] + '='
  }
  return out
}
