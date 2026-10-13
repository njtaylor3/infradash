"""The UI itself is out of scope here; this only checks it still runs on the refactored backend."""
import asyncio

from infradash.app import InfraDash
from infradash.collector import Collector
from infradash.demo import DemoProvider


def test_app_runs_headless_on_the_demo():
    async def main():
        app = InfraDash(Collector(DemoProvider()), refresh_s=0.2)
        async with app.run_test(size=(200, 60)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("s", "v", "n")
            await pilot.pause(0.5)
            return app.snap

    snap = asyncio.run(main())
    assert snap is not None and snap.raw.guests
