package org.athena.relay.net

import java.io.IOException
import java.net.URLDecoder

/**
 * Pairing with Odysseus (hub: Settings → Integrations → Pair a phone). The QR code holds
 * `odysseus-pair:v1?url=<Odysseus URL>&code=<pairing code>`: no token. The code is single use
 * and valid for 10 minutes; [PairingClient.redeem] trades it for this phone's own `ody_…`
 * API token, which the hub shows (and revokes) in Settings → API tokens.
 */
object PairingLink {
    const val PREFIX = "odysseus-pair:v1?"
    private val CODE = Regex("[A-Za-z0-9_-]{8,100}")

    data class Parsed(val base: String, val code: String, val warning: String?)

    sealed class Result {
        data class Ok(val link: Parsed) : Result()
        data class Invalid(val reason: String) : Result()
    }

    /** Parses a scanned or pasted link; the URL must pass [ServerUrl.check]. */
    fun parse(raw: String): Result {
        val text = raw.trim()
        if (!text.startsWith(PREFIX)) {
            return Result.Invalid("Not an Odysseus pairing code. In Odysseus open Settings → Integrations → Pair a phone.")
        }
        val fields = HashMap<String, String>()
        for (part in text.removePrefix(PREFIX).split('&')) {
            val k = part.substringBefore('=')
            val v = part.substringAfter('=', "")
            if (k.isNotEmpty() && k !in fields) {
                fields[k] = try {
                    URLDecoder.decode(v, "UTF-8")
                } catch (e: IllegalArgumentException) {
                    return Result.Invalid("The pairing link is damaged")
                }
            }
        }
        val code = fields["code"].orEmpty()
        if (!CODE.matches(code)) return Result.Invalid("The pairing link has no valid code")
        return when (val url = ServerUrl.check(fields["url"].orEmpty())) {
            is ServerUrl.Check.Ok -> Result.Ok(Parsed(url.base, code, url.warning))
            is ServerUrl.Check.Invalid -> Result.Invalid("Odysseus address in the code: ${url.reason}")
        }
    }
}

/** What redeeming answered: the token is shown once and must go straight to the Keystore. */
class Paired(val base: String, val token: String, val name: String) {
    override fun toString(): String = "Paired($base, $name)" // never the token
}

class PairingClient(private val transport: HttpTransport = UrlConnectionTransport()) {
    sealed class Result {
        data class Ok(val paired: Paired) : Result()
        /** The user can act on it: make a new code, wait, check the address. */
        data class Failed(val reason: String) : Result()
    }

    /**
     * `POST <base>/api/pair/redeem {"code": …}` on the URL from the code only, no redirects.
     * The code is consumed by the first try, so a network error after sending means making a
     * new code.
     */
    suspend fun redeem(link: PairingLink.Parsed): Result {
        val body = Json.write(mapOf("code" to link.code)).toByteArray(Charsets.UTF_8)
        val answer = try {
            transport.exchange("POST", link.base + PATH, mapOf("Content-Type" to "application/json"), body)
        } catch (e: IOException) {
            return Result.Failed("Odysseus not reachable at ${link.base} (${e.message ?: e.javaClass.simpleName}). " +
                "Is the phone on Tailscale?")
        }
        return when (answer.code) {
            200 -> parse(link, answer.body)
            429 -> Result.Failed("Too many tries. Wait a few minutes, then make a new code.")
            in 300..399 -> Result.Failed("Odysseus answered with a redirect (HTTP ${answer.code}); check the address.")
            404, 405 -> Result.Failed("This Odysseus can't pair phones yet (update the hub).")
            in 400..499 -> Result.Failed(JsonClient.detail(answer.body)
                ?: "The code was not accepted (HTTP ${answer.code}). Make a new one.")
            else -> Result.Failed("Odysseus error HTTP ${answer.code}; try again with a new code.")
        }
    }

    private fun parse(link: PairingLink.Parsed, body: String): Result {
        val obj = try {
            Json.parseObject(body)
        } catch (e: Json.ParseException) {
            return Result.Failed("Odysseus answered without JSON")
        }
        val token = obj.str("token").orEmpty()
        if (!token.startsWith("ody_") || token.length < 12) return Result.Failed("Odysseus answered without a token")
        return Result.Ok(Paired(link.base, token, obj.str("name") ?: "Phone relay"))
    }

    companion object {
        const val PATH = "/api/pair/redeem"
    }
}
