package org.barvis.sound.relay.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** docs/t5-pda-plan.md §3.3: the glance protocol, byte for byte (little-endian). */
class GlanceVectorsTest {
    private fun h(s: String) = hexToBytes(s)

    private fun roundTrip(hex: String, c: PdaCommand) {
        assertArrayEquals(h(hex), c.encode())
        assertEquals(c, PdaCommand.decode(h(hex)))
    }

    private fun roundTrip(hex: String, r: PdaResponse) {
        assertArrayEquals(h(hex), r.encode())
        assertEquals(r, PdaResponse.decode(h(hex)))
    }

    @Test fun time() {
        // 2026-10-19T12:00:00Z, UTC+2 (120 min) and UTC-5 (-300 min).
        roundTrip("04 000000 006a0854a1010000 7800", PdaCommand.Time(1792411200000L, 120))
        roundTrip("04 000000 006a0854a1010000 d4fe", PdaCommand.Time(1792411200000L, -300))
    }

    @Test fun put() {
        roundTrip("10 01 0000 80b81467 34120000 2639f4cb", PdaCommand.PutBegin(1, 1729411200L, 0x1234, 0xcbf43926L))
        roundTrip("11 00010000 616263", PdaCommand.PutData(256, "abc".toByteArray()))
        roundTrip("12", PdaCommand.PutEnd)
    }

    @Test fun outbox() {
        roundTrip("20 00000000 1000", PdaCommand.ObList(0, 16))
        roundTrip("21 07000000 01", PdaCommand.ObAck(7, AckResult.REJECTED))
        roundTrip("05", PdaCommand.Bye)
    }

    @Test fun responses() {
        roundTrip("86 000000", PdaResponse.TimeOk)
        roundTrip("87", PdaResponse.ByeOk)
        roundTrip("90 02 0000 05000000", PdaResponse.PutReady(BlobKind.DETAIL, 5))
        roundTrip("91 01 0000 80b81467", PdaResponse.PutOk(BlobKind.FEED, 1729411200L))
        roundTrip("a1 00 0300 02000000", PdaResponse.ObEnd(3, 2))
        roundTrip("a2 00 0000 07000000", PdaResponse.ObAcked(0, 7))
        roundTrip("ee 12 08 00 00000000", PdaResponse.Error(PdaCmd.PUT_END, PdaErrorCode.BAD_CRC))
        assertEquals(PdaErrorCode.BAD_STATE, (PdaResponse.decode(h("ee 11 0a 00 00000000")) as PdaResponse.Error).code)
        assertEquals(PdaErrorCode.UNKNOWN, (PdaResponse.decode(h("ee 11 63 00 00000000")) as PdaResponse.Error).code)
    }

    @Test fun outboxEntry() {
        val json = """{"t":"item_status","id":42,"status":"done"}"""
        val hex = "a0 00 2b00 09000000 006a0854a1010000" + json.toByteArray().toHex()
        val e = OutboxEntry(9, 1792411200000L, json.toByteArray())
        assertArrayEquals(h(hex), e.encode())
        val d = (PdaResponse.decode(h(hex)) as PdaResponse.Entry).entry
        assertEquals(e, d)
        assertEquals(json, d.text)
    }

    @Test fun outboxEntryLengthIsChecked() {
        try {
            PdaResponse.decode(h("a0 00 2b00 09000000 006a0854a1010000 7b7d"))
            throw AssertionError("short entry accepted")
        } catch (e: ProtocolException) {
            assertTrue(e.message!!.contains("43"))
        }
    }

    @Test fun status() {
        val hex = "01 13 50 00 0200 0000 80b81467 04030201"
        val s = PdaStatus.decode(h(hex))
        assertEquals(PdaStatus(1, 0x13, 80, 2, 1729411200L, 0x01020304), s)
        assertTrue(s.timeValid && s.charging && s.userActive)
        assertFalse(s.usbPower || s.pairingWindowOpen)
        assertArrayEquals(h(hex), s.encode())
        assertEquals(-1, PdaStatus.decode(h("01 00 ff 00 0000 0000 00000000 00000000")).batteryPercent)
    }

    @Test fun infoUsesTheWatchParser() {
        val i = WatchInfo.parse("proto=1;device=t5-pda;fw=0.1.0;board=t5_epaper_pro")
        assertEquals(1, i.proto)
        assertEquals("t5-pda", i.device)
        assertEquals("t5_epaper_pro", i.board)
    }
}
