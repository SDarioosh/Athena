/*
 * Loads a watch page (page/*.js) in Node with the fake Zepp OS modules from zos.mjs.
 * The app's imports are extensionless ('../shared/enroll'), as zeus resolves them, so
 * this adds '.js'. Each loadPage() call evaluates the page module afresh and returns
 * the object the page passed to Page(), with `request` set to the given fake.
 */
import { registerHooks } from 'node:module'
import { fake } from './zos.mjs'

const FAKE = new URL('./zos.mjs', import.meta.url).href
const APP = new URL('../../', import.meta.url).href

registerHooks({
  resolve(spec, ctx, next) {
    if (spec.startsWith('@zos/') || spec === '@zeppos/zml/base-page') {
      return { url: FAKE, shortCircuit: true }
    }
    if (spec.startsWith('.') && !/\.[cm]?js$/.test(spec) && ctx.parentURL
        && ctx.parentURL.startsWith(APP)) {
      return next(`${spec}.js`, ctx)
    }
    return next(spec, ctx)
  },
})

let n = 0
export async function loadPage(name, request = async () => ({ ok: true })) {
  let def = null
  globalThis.Page = (d) => {
    def = d
  }
  await import(`${APP}page/${name}.js?v=${++n}`)
  def.request = request
  return def
}

export { fake }
