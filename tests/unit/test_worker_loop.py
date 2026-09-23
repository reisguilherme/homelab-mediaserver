import asyncio

from homeserver_control.worker.__main__ import _run_forever


def test_worker_reuses_one_event_loop_for_repeated_http_cycles() -> None:
    class LoopBoundCycle:
        def __init__(self) -> None:
            self.first_loop: asyncio.AbstractEventLoop | None = None
            self.calls = 0
            self.source = object()

        async def run_once(self) -> None:
            loop = asyncio.get_running_loop()
            if self.first_loop is None:
                self.first_loop = loop
            assert loop is self.first_loop
            self.calls += 1

    cycle = LoopBoundCycle()
    asyncio.run(_run_forever(cycle, should_run=lambda: cycle.calls < 2, interval=0))
    assert cycle.calls == 2
