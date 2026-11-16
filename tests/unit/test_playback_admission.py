"""A short-lived platform lease controls whether playback may serve media."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/playback-admission.sh'
BOOT_ID = 'ca9a5731-8b28-4400-8ee9-138e1485de61'
GENERATION = '21ea0f809f77421a9ce2a040376a43da'


def lease_text(*, state='ready', boot_id=BOOT_ID, generation=GENERATION, expires=145):
    return (
        f'version=1\nstate={state}\nboot_id={boot_id}\ngeneration={generation}\n'
        f'expires_uptime_seconds={expires}\n'
    )


def check(tmp_path, *, lease=None, boot=None, uptime='100.00 90.00\n'):
    values = [lease_text() if lease is None else lease,
              BOOT_ID + '\n' if boot is None else boot, uptime]
    paths = [tmp_path / name for name in ['lease', 'boot', 'uptime']]
    for path, value in zip(paths, values, strict=True):
        path.write_bytes(value.encode('utf-8'))
    return subprocess.run(['sh', str(SCRIPT), *map(str, paths)],
                          text=True, capture_output=True, timeout=5)


@pytest.mark.parametrize('uptime, expires', [('100.00', 101), ('100.99', 101), ('100.00', 145)])
def test_ready_lease_matches_this_boot_and_short_remaining_lifetime(tmp_path, uptime, expires):
    result = check(tmp_path, lease=lease_text(expires=expires), uptime=f'{uptime} 90.00\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f'{BOOT_ID}:{GENERATION}'


def test_renewal_does_not_change_generation_snapshot(tmp_path):
    before = check(tmp_path)
    after = check(tmp_path, lease=lease_text(expires=175), uptime='130.00 95.00\n')
    assert before.returncode == after.returncode == 0
    assert before.stdout == after.stdout


@pytest.mark.parametrize('lease', [
    '',
    lease_text(state='hold', expires=0),
    lease_text(state='hold'),
    lease_text(state='READY'),
    lease_text(boot_id='ca9a5731-8b28-4400-8ee9-138e1485de62'),
    lease_text(boot_id=BOOT_ID.upper()),
    lease_text(generation=GENERATION.upper()),
    lease_text(generation=GENERATION[:-1]),
    lease_text(generation='z' * 32),
    lease_text(expires=100),
    lease_text(expires=99),
    lease_text(expires=146),
    lease_text(expires='1e2'),
    lease_text(expires='0145'),
    lease_text(expires='145.0'),
    lease_text(expires='-1'),
    lease_text().replace('version=1', 'version=2'),
    lease_text().replace('state=ready', 'state=ready\x00'),
    lease_text().replace('state=ready', 'state=réady'),
    lease_text().replace('state=ready', 'state=ready '),
    lease_text().replace('state=ready', 'state=ready\t'),
    lease_text().replace('\n', '\r\n'),
    lease_text().removesuffix('\n'),
    lease_text() + '\n',
    lease_text() + 'state=hold\n',
    lease_text().replace('version=1\nstate=ready', 'state=ready\nversion=1'),
    '\ufeff' + lease_text(),
    'version=1' + ' ' * 1024 + '\n' + lease_text().split('\n', 1)[1],
])
def test_hold_expired_foreign_boot_or_malformed_lease_is_refused(tmp_path, lease):
    result = check(tmp_path, lease=lease)
    assert result.returncode != 0
    assert result.stdout == ''


@pytest.mark.parametrize('uptime', [
    '', '100', '100.00\n', 'nan 90.00\n', '-1.00 90.00\n', '1e2 90.00\n',
    '100.00 90.00\n\n', '100.00 90.00\x00\n', '100.00 90.00',
])
def test_invalid_monotonic_clock_is_refused(tmp_path, uptime):
    result = check(tmp_path, uptime=uptime)
    assert result.returncode != 0


@pytest.mark.parametrize('boot', [
    '', BOOT_ID, BOOT_ID.upper() + '\n', BOOT_ID + '\n\n', BOOT_ID + '\x00\n',
    'ca9a5731-8b28-4400-8ee9-138e1485de62\n',
])
def test_invalid_or_changed_guest_boot_is_refused(tmp_path, boot):
    result = check(tmp_path, boot=boot)
    assert result.returncode != 0


@pytest.mark.parametrize('name', ['lease', 'boot', 'uptime'])
def test_missing_input_is_refused(tmp_path, name):
    assert check(tmp_path).returncode == 0
    (tmp_path / name).unlink()
    result = subprocess.run(
        ['sh', str(SCRIPT), *[str(tmp_path / item) for item in ['lease', 'boot', 'uptime']]],
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode != 0


def test_new_generation_changes_runtime_identity(tmp_path):
    before = check(tmp_path)
    after = check(tmp_path, lease=lease_text(generation='bc7e260afda04822a3a6f14a33a36970'))
    assert before.returncode == after.returncode == 0
    assert before.stdout != after.stdout
