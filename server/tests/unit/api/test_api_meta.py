"""The API's own name and version (OpenAPI info)."""

from importlib import metadata

from athena import api


def test_app_title_and_version_from_package(client) -> None:
    assert client.app.title == "Athena server"
    assert client.app.version == metadata.version("athena-server")


def test_version_falls_back_when_not_installed(monkeypatch) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(api.metadata, "version", missing)
    assert api._package_version() == "0.0.0"


def test_version_read_from_the_old_distribution_name(monkeypatch) -> None:
    """A venv installed before the rename has only the ``convo-server`` distribution."""
    def old_only(name: str) -> str:
        if name == "convo-server":
            return "0.1.0"
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(api.metadata, "version", old_only)
    assert api._package_version() == "0.1.0"
