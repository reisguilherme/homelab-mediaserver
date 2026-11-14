from types import SimpleNamespace

import pytest

from homeserver_control.worker.acquisition import MovieAcquirer


@pytest.mark.parametrize("base,url,allowed", [
    ("http://prowlarr:9696", "http://prowlarr:9696/6/download?apikey=fixture", True),
    ("http://prowlarr:9696/prowlarr", "http://prowlarr:9696/prowlarr/6/download?apikey=x", True),
    ("http://prowlarr:9696/prowlarr", "http://prowlarr:9696/6/download", False),
    ("http://prowlarr:9696/prowlarr", "http://prowlarr:9696/prowlarr/../6/download", False),
    ("http://prowlarr:9696/prowlarr", "http://prowlarr:9696/prowlarr/%2e%2e/6/download", False),
    ("http://prowlarr:9696/prowlarr", "http://example.test/prowlarr/6/download", False),
    ("http://prowlarr:9696/prowlarr", "http://user:pw@prowlarr:9696/prowlarr/6/download", False),
    ("http://prowlarr:9696/prowlarr", "http://prowlarr:9696/prowlarr/6/download#foo", False),
])
def test_download_url_keeps_strict_host_and_native_prefix(base, url, allowed):
    acquirer = SimpleNamespace(prowlarr_url=base)
    assert MovieAcquirer._trusted_download_url(acquirer, url) is allowed
