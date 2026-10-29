"""The API's own name and version (OpenAPI info)."""

from importlib import metadata

from convo_server import api


def test_app_title_and_version_from_package(client) -> None:
    assert client.app.title == "Athena server"
    assert client.app.version == metadata.version("convo-server")


def test_version_falls_back_when_not_installed(monkeypatch) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(api.metadata, "version", missing)
    assert api._package_version() == "0.0.0"
