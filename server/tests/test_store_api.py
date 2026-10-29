"""``Store`` keeps its public methods and their signatures when its code moves.

``fixtures/store_public_api.json`` lists every public method of ``Store`` with its
``inspect.signature``, taken from ``store.py`` before it was split into ``athena/db/``.
Callers use ``Store`` only through these, so each must still be there, unchanged. The
``UNSET`` default ("not given") prints as an address, so the list writes it ``UNSET``.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from athena.store import UNSET, Store

EXPECTED = json.loads((Path(__file__).parent / "fixtures" / "store_public_api.json")
                      .read_text())


def test_the_list_is_whole() -> None:
    assert len(EXPECTED) == 112


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_public_method_keeps_its_signature(name: str) -> None:
    method = getattr(Store, name, None)
    assert callable(method), f"Store.{name} is gone"
    signature = inspect.signature(method)
    shown = re.sub(r"<object object at 0x[0-9a-f]+>", "UNSET", str(signature))
    assert shown == EXPECTED[name]
    for p in signature.parameters.values():  # "UNSET" above is the one sentinel
        if "<object object" in repr(p.default):
            assert p.default is UNSET

