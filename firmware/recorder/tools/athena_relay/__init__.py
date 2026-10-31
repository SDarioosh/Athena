"""Relay logic for the T-Watch Ultra BLE chunk-transfer protocol (docs/twatch-ble-protocol.md).

Independent of bleak so it can be tested with a fake watch: `protocol` (message codec),
`uploader` (server client, standard library only), `drain` (the relay algorithm, §6),
`anchors` (TIME_UNSET session anchors, §2.3).
"""
