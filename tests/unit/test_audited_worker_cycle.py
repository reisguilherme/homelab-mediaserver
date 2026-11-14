import pytest

from homeserver_control.worker.runtime import CycleReport, WorkerCycle


class Forbidden:
    def __init__(self):
        self.calls = []

    async def list_approved(self, _page):
        self.calls.append('requests')
        raise AssertionError('audited imports must not query Seerr')

    def admit(self, _candidate):
        self.calls.append('admit')
        raise AssertionError('audited imports must not select or reserve sources')

    async def reconcile(self, *_args):
        self.calls.append('reconcile')
        raise AssertionError('audited imports must not cancel operator reservations')

    async def run_once(self):
        self.calls.append('mutate')
        raise AssertionError('normal mutations must not run in audited mode')


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
@pytest.mark.parametrize('artwork_fails', [False, True])
async def test_audited_callback_reconciles_artwork_without_normal_mutations(
    fails, artwork_fails, caplog,
):
    seen = []

    async def capacity():
        seen.append('capacity')

    class Phase:
        async def run_once(self):
            seen.append('audited')
            if fails:
                raise RuntimeError('must remain fail closed')

    class Covers:
        async def reconcile(self):
            seen.append('artwork')
            if artwork_fails:
                raise RuntimeError('private artwork credential details')

    forbidden = Forbidden()
    cycle = WorkerCycle(
        source=forbidden, scheduler=forbidden, cancellation=forbidden,
        deletion_coordinator=forbidden, source_reconciler=forbidden,
        jellyfin_collections=Covers(), capacity_refresh=capacity, audited_imports=Phase(),
    )
    assert await cycle.run_once() == CycleReport()
    assert seen == ['capacity', 'artwork', 'audited']
    assert forbidden.calls == []
    assert 'private artwork credential details' not in caplog.text


@pytest.mark.asyncio
async def test_default_cycle_keeps_normal_behavior():
    class Empty(Forbidden):
        async def list_approved(self, _page):
            return []

    cycle = WorkerCycle(source=Empty(), scheduler=Forbidden())
    assert await cycle.run_once() == CycleReport()
