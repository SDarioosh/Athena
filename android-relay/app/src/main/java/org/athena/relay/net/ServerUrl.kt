package org.athena.relay.net

import java.net.URI
import java.net.URISyntaxException

/**
 * Validates the server URL from the settings and decides whether a cleartext (`http://`)
 * URL deserves a warning. The relay only ever talks to this one URL; there is no fallback.
 */
object ServerUrl {
    sealed class Check {
        /** [base] has no trailing slash. [warning] is set for http:// to a non-private host. */
        data class Ok(val base: String, val warning: String?) : Check()
        data class Invalid(val reason: String) : Check()
    }

    fun check(raw: String): Check {
        val text = raw.trim()
        if (text.isEmpty()) return Check.Invalid("Enter the server URL")
        val uri = try {
            URI(text)
        } catch (e: URISyntaxException) {
            return Check.Invalid("Not a valid URL")
        }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return Check.Invalid("The URL must start with http:// or https://")
        val host = uri.host ?: return Check.Invalid("The URL has no host")
        if (uri.rawUserInfo != null) return Check.Invalid("Don't put a user name or password in the URL")
        if (uri.rawQuery != null || uri.rawFragment != null) return Check.Invalid("Remove the ?query or #fragment")
        val path = (uri.rawPath ?: "").trimEnd('/')
        val port = if (uri.port >= 0) ":${uri.port}" else ""
        val base = "$scheme://$host$port$path"
        val warning = if (scheme == "http" && !isPrivateHost(host)) {
            "Unencrypted http:// to $host, which isn't a LAN, Tailscale (100.64.0.0/10, *.ts.net) " +
                "or .local address. The token and audio could be read on the way. Use https:// " +
                "or the server's Tailscale address."
        } else null
        return Check.Ok(base, warning)
    }

    /**
     * True for addresses that stay on the owner's network: RFC 1918, Tailscale CGNAT
     * 100.64.0.0/10, loopback, IPv6 ULA (incl. Tailscale fd7a:115c:a1e0::/48) and link-local,
     * `*.ts.net`, `*.local`, `localhost`. No DNS lookups: names are judged by their suffix.
     */
    fun isPrivateHost(rawHost: String): Boolean {
        val host = rawHost.trim().removePrefix("[").removeSuffix("]").lowercase().trimEnd('.')
        if (host == "localhost" || host.endsWith(".ts.net") || host.endsWith(".local")) return true
        parseIpv4(host)?.let { o ->
            return o[0] == 10 ||
                (o[0] == 172 && o[1] in 16..31) ||
                (o[0] == 192 && o[1] == 168) ||
                (o[0] == 100 && o[1] in 64..127) ||
                o[0] == 127
        }
        if (host.contains(':')) {
            if (host == "::1") return true
            val first = host.substringBefore(':').ifEmpty { "0" }.toIntOrNull(16) ?: return false
            return (first and 0xFE00) == 0xFC00 || (first and 0xFFC0) == 0xFE80
        }
        return false
    }

    private fun parseIpv4(host: String): IntArray? {
        val parts = host.split('.')
        if (parts.size != 4) return null
        val out = IntArray(4)
        for (i in 0 until 4) {
            val p = parts[i]
            if (p.isEmpty() || p.length > 3 || !p.all { it.isDigit() }) return null
            out[i] = p.toInt()
            if (out[i] > 255) return null
        }
        return out
    }
}
