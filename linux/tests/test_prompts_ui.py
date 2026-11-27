#!/usr/bin/env python3
"""The PC's permission prompts: an answer is passed on, and a prompt nobody answered in time is taken off the screen
(its buttons would no longer do anything). Real dialogs on a (virtual) display.
Run directly: `xvfb-run -a python3 tests/test_prompts_ui.py`
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import app as appmod  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


async def pump(seconds: float = 0.3) -> None:
    """Runs the GTK main context alongside asyncio."""
    context = appmod.GLib.MainContext.default()
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        while context.iteration(False):
            pass
        await asyncio.sleep(0.01)


async def main_async() -> int:
    adw = appmod.Adw
    adw.init()
    shown: list = []

    def record(**kw):
        shown.append(adw.MessageDialog(**kw))
        return shown[-1]

    app = appmod.PhoneBridgeApp()
    appmod.Adw = types.SimpleNamespace(MessageDialog=record, ResponseAppearance=adw.ResponseAppearance)

    class Device:
        name = "Test Phone"

    prompts = (("permission", lambda: app._ask_permission(Device(), "files"), "allow", True),  # noqa: SLF001
               ("PC control", lambda: app._ask_pc_control(Device()), "15", 15))  # noqa: SLF001
    for name, ask, response, expected in prompts:
        shown.clear()
        task = asyncio.ensure_future(ask())
        await pump()
        check(f"{name}: a prompt is on screen", len(shown) == 1 and shown[0].get_visible())
        shown[0].response(response)
        await pump()
        check(f"{name}: the answer is passed on", task.done() and task.result() == expected)

        shown.clear()
        try:
            task = asyncio.ensure_future(asyncio.wait_for(ask(), 2.0))   # the caller's one-minute limit, shortened
            await pump(0.5)
            visible_before = len(shown) == 1 and shown[0].get_visible()
            await pump(2.0)
            await task
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
        check(f"{name}: an unanswered prompt times out and is removed",
              visible_before and timed_out and not shown[0].get_visible())

        shown.clear()
        task = asyncio.ensure_future(ask())
        await asyncio.sleep(0)       # the request is queued for the GTK thread, which has not run yet
        task.cancel()
        await pump()
        check(f"{name}: a request abandoned before it was drawn shows nothing", shown == [])

    appmod.Adw = adw
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
