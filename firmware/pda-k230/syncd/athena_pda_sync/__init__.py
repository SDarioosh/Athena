"""athena-pda-syncd: the K230 PDA's own relay (docs/k230-pda-plan.md).

It connects to the UI's `glance.sock` (Contract 1), pairs with Odysseus by the hub's
"Pair a phone" code, and runs `athena_relay.pda_sync.PdaSync` over the socket against the
hub's token routes. Standard library only.

`athena_relay` (firmware/recorder/tools/athena_relay) is imported from where it is, never
copied: `relay_path.ensure()` finds it (the one seam, see there) before any module here
imports it.
"""

from . import relay_path

relay_path.ensure()

__version__ = "0.1.0"
