/*
 * Fake Zepp OS modules for page tests in Node. tests/fakes/pages.mjs maps every
 * `@zos/*` import and `@zeppos/zml/base-page` to this one file, so it exports the
 * union of the names the app imports. Tests read and steer it through `fake`.
 */
export const fake = {
  widgets: [], // every widget created: {type, props, deleted, text}
  files: new Map(), // data:// file name -> Uint8Array
  fds: new Map(), // open fd -> file name
  nextFd: 3,
  openResult: null, // when set, openSync returns this instead of a real fd
  readResult: null, // when set, readSync returns this instead of the bytes read
  readThrows: null, // when set, readSync throws this
  pushed: [],
  storage: new Map(),
  permission: 2, // GRANTED
  recorders: [],
  reset() {
    this.widgets = []
    this.files = new Map()
    this.fds = new Map()
    this.nextFd = 3
    this.openResult = null
    this.readResult = null
    this.readThrows = null
    this.pushed = []
    this.storage = new Map()
    this.permission = 2
    this.recorders = []
  },
  live() {
    return this.widgets.filter((w) => !w.deleted)
  },
  button(text) {
    return this.live().find((w) => w.type === 'BUTTON' && w.props.text === text)
  },
}

// @zos/ui
export const widget = { BUTTON: 'BUTTON', TEXT: 'TEXT' }
export const prop = { TEXT: 'TEXT' }
export const align = { CENTER_H: 'CENTER_H' }
export const text_style = { WRAP: 'WRAP' }
export function createWidget(type, props) {
  const w = {
    type, props, deleted: false, text: props.text,
    setProperty(p, v) {
      if (p === prop.TEXT) this.text = v
    },
  }
  fake.widgets.push(w)
  return w
}
export function deleteWidget(w) {
  w.deleted = true
}

// @zos/utils
export const px = (n) => n

// @zos/media
export const id = { RECORDER: 'RECORDER' }
export const codec = { OPUS: 'OPUS' }
export function create() {
  const r = {
    started: 0, stopped: 0, file: null,
    event: { START: 'START' },
    state: { IDLE: 0, PREPARING: 1, STARTING: 2, RECORDING: 3 },
    addEventListener() {},
    setFormat(_c, opts) {
      this.file = opts.target_file
    },
    start() {
      this.started += 1
    },
    stop() {
      this.stopped += 1
    },
    getStatus() {
      return this.started > this.stopped ? 3 : 0
    },
  }
  fake.recorders.push(r)
  return r
}

// @zos/fs
export const O_RDONLY = 0
export function statSync({ path }) {
  const f = fake.files.get(path)
  return f ? { size: f.length } : undefined
}
export function openSync({ path }) {
  if (fake.openResult !== null) return fake.openResult
  if (!fake.files.has(path)) return -1
  const fd = fake.nextFd++
  fake.fds.set(fd, path)
  return fd
}
export function readSync({ fd, buffer }) {
  if (fake.readThrows) throw fake.readThrows
  const path = fake.fds.get(fd)
  if (path === undefined) return -1
  const src = fake.files.get(path)
  const n = Math.min(src.length, buffer.byteLength)
  new Uint8Array(buffer).set(src.subarray(0, n))
  return fake.readResult !== null ? fake.readResult : n
}
export function closeSync(arg) {
  const fd = typeof arg === 'object' && arg ? arg.fd : arg
  fake.fds.delete(fd)
  return 0
}
export function rmSync({ path }) {
  fake.files.delete(path)
}

// @zos/router
export function push(opts) {
  fake.pushed.push(opts)
}
export function back() {}

// @zos/storage
export class LocalStorage {
  getItem(k) {
    return fake.storage.has(k) ? JSON.parse(fake.storage.get(k)) : undefined
  }
  setItem(k, v) {
    fake.storage.set(k, JSON.stringify(v))
  }
}

// @zos/display
export function setPageBrightTime() {}
export function resetPageBrightTime() {}
export function pauseDropWristScreenOff() {}
export function resetDropWristScreenOff() {}
export function pausePalmScreenOff() {}
export function resetPalmScreenOff() {}
export function setWakeUpRelaunch() {}

// @zos/app
export function queryPermission() {
  return [fake.permission]
}
export function requestPermission() {
  return fake.permission
}

// @zos/sensor
export class Vibrator {
  start() {}
}

// @zeppos/zml/base-page: the test supplies request() on the page object.
export function BasePage(def) {
  return def
}
