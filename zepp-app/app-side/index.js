/*
 * Side Service (runs inside the Zepp app on the phone).
 * - 'ping': GET <server>/health, to check the phone can reach the server (Tailscale).
 * - binary envelope from the watch: POST the chunk to <server>/v1/ingest/chunk.json, or a
 *   voice-profile recording (header kind "enroll") to <server>/v1/speakers/enroll.json.
 * - 'end': tell the server the session is finished.
 * Server URL and token come from the app's Settings page in the Zepp app.
 */
import { BaseSideService } from '@zeppos/zml/base-side'
import { ping, uploadEnvelope, endSession } from '../shared/uploader'

function config() {
  const get = (k) => (settings.settingsStorage.getItem(k) || '').trim()
  return { url: get('serverUrl').replace(/\/+$/, ''), token: get('token'),
           ownerName: get('ownerName') }
}

AppSideService(
  BaseSideService({
    onInit() {},
    onRun() {},
    onDestroy() {},
    onRequest(req, res) {
      const isBinary = req instanceof ArrayBuffer || (req && req.byteLength !== undefined && !req.method)
      let work
      if (isBinary) work = uploadEnvelope(fetch, config(), req)
      else if (req.method === 'ping') work = ping(fetch, config())
      else if (req.method === 'end') work = endSession(fetch, config(), req.params && req.params.session_id)
      else work = Promise.resolve({ ok: false, error: `unknown request ${req.method}` })
      work.then((result) => res(null, result)).catch((e) => res(null, { ok: false, error: String(e) }))
    },
  }),
)
