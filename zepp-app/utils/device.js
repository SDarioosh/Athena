/*
 * Watch-side helpers shared by the pages: microphone permission, the recorder,
 * display policy, files and vibration. Moved unchanged from page/index.js.
 */
import { create, id } from '@zos/media'
import {
  setPageBrightTime, resetPageBrightTime, pauseDropWristScreenOff, resetDropWristScreenOff,
  pausePalmScreenOff, resetPalmScreenOff, setWakeUpRelaunch,
} from '@zos/display'
import { statSync, openSync, readSync, closeSync, O_RDONLY } from '@zos/fs'
import { queryPermission, requestPermission } from '@zos/app'
import { Vibrator } from '@zos/sensor'

let recorder = null
export function getRecorder(onFail) {
  if (recorder) return recorder
  const r = create(id.RECORDER)
  if (!r) throw new Error('create(id.RECORDER) returned nothing')
  try {
    r.addEventListener(r.event.START, (ok) => {
      if (ok === false) onFail('recorder START event reported failure')
    })
  } catch (e) {
    console.log('recorder event listener not available', e)
  }
  recorder = r
  return r
}

const MIC = 'device:os.mic'
const GRANTED = 2

function micStatus() {
  return queryPermission({ permissions: [MIC] })[0]
}

// Calls onGranted once the app may use the microphone, or onDenied with a reason.
// Listing the permission in app.json is not always enough: the watch can still answer
// "PERMISSION DENIED device:os.mic" until the user allows it in a system prompt.
export function withMic(onGranted, onDenied) {
  let done = false
  const finish = (ok, why) => {
    if (done) return
    done = true
    if (ok) onGranted()
    else onDenied(why)
  }
  let status
  try {
    status = micStatus()
  } catch (e) {
    // No permission API on this firmware: try the recorder and let it report.
    finish(true)
    return
  }
  if (status === GRANTED) return finish(true)
  const result = requestPermission({
    permissions: [MIC],
    callback: () => {
      let now = null
      try {
        now = micStatus()
      } catch (e) {}
      finish(now === GRANTED, 'microphone not allowed (answer Allow on the watch prompt)')
    },
  })
  if (result === GRANTED) finish(true)
  else if (result === 1) {
    finish(false, 'microphone cannot be requested: allow it for Convo Rec Test in the watch or Zepp app settings, or reinstall')
  }
  // result 0: the watch shows a prompt and the callback decides.
}

export function errText(e) {
  return e && e.message ? e.message : String(e)
}

// Runs one recorder call and names it in the error, so the screen says which step failed.
export function step(name, fn) {
  try {
    return fn()
  } catch (e) {
    throw new Error(`${name}: ${errText(e)}`)
  }
}

const STATUS_NAMES = ['IDLE', 'PREPARING', 'STARTING', 'RECORDING']

export function recorderStatus() {
  if (!recorder || typeof recorder.getStatus !== 'function') return null
  const status = recorder.getStatus()
  const states = recorder.state
  if (!states || states.RECORDING === undefined) return null
  const name = STATUS_NAMES.find((k) => states[k] === status)
  return { status, name: name || `unknown ${status}`, recording: status === states.RECORDING }
}

export function fileSize(file) {
  try {
    const st = statSync({ path: file })
    if (st && st.size !== undefined) return st.size
  } catch (e) {}
  return undefined
}

export function readFile(file) {
  const size = fileSize(file)
  if (!size) return null
  const fd = openSync({ path: file, flag: O_RDONLY })
  // A bad handle or a short read would upload a zero-filled buffer as audio.
  if (typeof fd !== 'number' || fd < 0) throw new Error(`open ${file} failed (${fd})`)
  try {
    const buf = new ArrayBuffer(size)
    const n = readSync({ fd, buffer: buf })
    if (typeof n === 'number' && n !== size) throw new Error(`read ${n} of ${size} bytes`)
    return buf
  } finally {
    closeSync(fd)
  }
}

export function applyDisplay(mode) {
  try {
    setWakeUpRelaunch({ relaunch: true })
    if (mode === 'screen-on') {
      setPageBrightTime({ brightTime: 2147483000 })
      pauseDropWristScreenOff({ duration: 0 })
      pausePalmScreenOff({ duration: 0 })
    }
  } catch (e) {
    console.log('display policy failed', e)
  }
}

export function resetDisplay() {
  try {
    resetPageBrightTime()
    resetDropWristScreenOff()
    resetPalmScreenOff()
    setWakeUpRelaunch({ relaunch: false })
  } catch (e) {}
}

export function vibrate() {
  try {
    new Vibrator().start()
  } catch (e) {}
}

// The page that created the recorder is gone; the next start creates a new one.
export function releaseRecorder() {
  recorder = null
}

export function currentRecorder() {
  return recorder
}
