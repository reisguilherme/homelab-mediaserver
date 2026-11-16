"""Abyss assets must not broaden the public administrative surface."""

import pytest

from homeserver_control.api.public_policy import anonymous, permitted, target


@pytest.mark.parametrize('path', [
    '/web/ui/spotlight.html', '/jellyfin/web/ui/spotlight.html',
    '/Branding/Css', '/jellyfin/Branding/Css',
    '/jellyfin/web/ui/abyss.css', '/jellyfin/web/ui/spotlight-loader.js',
])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_abyss_only_exact_public_assets(path, method):
    normalized, _ = target(path, 'jellyfin')
    assert anonymous('jellyfin', normalized, method)
    assert permitted('jellyfin', normalized, method)


@pytest.mark.parametrize('path', [
    '/web/ui/other.html', '/web/ui/spotlight.html/other', '/web/ui/spotlight.html.bak',
    '/Branding/Configuration', '/System/Configuration/Branding', '/Branding/Css/other',
    '/System/Restart', '/Items/id/Images/Backdrop',
])
def test_abyss_does_not_publish_other_html_config_or_private_images(path):
    normalized, _ = target(path, 'jellyfin')
    assert not anonymous('jellyfin', normalized, 'GET')


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('path', ['/web/ui/spotlight.html', '/branding/css'])
def test_abyss_assets_cannot_be_mutated_publicly(path, method):
    assert not permitted('jellyfin', path, method)
