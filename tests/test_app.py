"""The UIs are out of scope here; these only check they still run on the refactored backend."""
import asyncio

from infradash.app import InfraDash
from infradash.btop.app import BtopApp
from infradash.collector import Collector
from infradash.demo import DemoProvider


def test_classic_app_runs_headless_on_the_demo():
    async def main():
        app = InfraDash(Collector(DemoProvider()), refresh_s=0.2)
        async with app.run_test(size=(200, 60)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("s", "v", "n")
            await pilot.pause(0.5)
            return app.snap

    snap = asyncio.run(main())
    assert snap is not None and snap.raw.guests


def test_btop_app_runs_headless_on_the_demo():
    async def main():
        app = BtopApp(Collector(DemoProvider()), refresh_s=0.5)
        async with app.run_test(size=(200, 60)) as pilot:
            await pilot.pause(1.2)
            await pilot.press("1", "1", "tab", "e", "plus", "minus", "h")
            await pilot.pause(0.3)
            await pilot.press("escape", "a")
            await pilot.pause(0.6)
            return app.snap

    snap = asyncio.run(main())
    assert snap is not None and snap.raw.guests
