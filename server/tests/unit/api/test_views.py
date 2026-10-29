"""athena.views: the /view page, apart from the links that open it (its bytes are pinned
by the contract snapshots view_*.html and relay_export__html.html)."""

from __future__ import annotations

import pytest

from athena import links, views


@pytest.mark.parametrize("name", ["SECTIONS", "render_conversation", "title_of", "when_of",
                                  "_no_summary", "_written", "_meeting"])
def test_old_names_still_read_from_links(name: str) -> None:
    assert getattr(links, name) is getattr(views, name)


def test_links_has_no_other_names() -> None:
    with pytest.raises(AttributeError):
        links.not_a_name  # noqa: B018


def test_links_does_not_load_the_page() -> None:
    import subprocess
    import sys

    code = "import sys, athena.links; print('athena.views' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "False"
