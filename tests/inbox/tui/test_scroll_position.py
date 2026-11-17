import asyncio

import pytest
from textual import events
from textual.app import App
from textual.scrollbar import ScrollTo
from textual.widgets import Static

from lemonaid.inbox.tui import scroll_position
from lemonaid.inbox.tui.tables.scroll_position import WHEEL_SECONDS, StableTable


@pytest.mark.parametrize("surface", ["markdown", "table"])
@pytest.mark.parametrize("command", ["wheel_down", "wheel_up", "drag"])
def test_pointer_scroll_supersedes_pending_restore(surface, command):
    async def check():
        app = App()
        async with app.run_test(size=(40, 15)) as pilot:
            if surface == "markdown":
                view = scroll_position.StableScroll()
                await app.mount(view)
                await view.mount(Static("\n".join(str(i) for i in range(80))))
            else:
                view = StableTable()
                await app.mount(view)
                view.add_column("Text")
                for i in range(80):
                    view.add_row(str(i), key=str(i))
            await pilot.pause()
            view.scroll_to(y=30, animate=False, immediate=True)
            await pilot.pause()
            if surface == "markdown":
                await scroll_position.replace(
                    view, [Static("\n".join(str(i) for i in range(80)))], True
                )
            else:
                with view.preserve_scroll():
                    view.clear()
                    for i in range(80):
                        view.add_row(str(i), key=str(i))
            if command == "wheel_down":
                view._scroll_down_for_pointer(animate=False)
            elif command == "wheel_up":
                view._scroll_up_for_pointer(animate=False)
            else:
                view._on_scroll_to(ScrollTo(y=45, animate=False))
            requested = view.scroll_y if command != "drag" or surface == "table" else 45
            await pilot.pause()
            assert view.scroll_y == requested

    asyncio.run(check())


def _wheel(view: StableTable, down: bool) -> None:
    kind = events.MouseScrollDown if down else events.MouseScrollUp
    view.post_message(kind(view, 1, 1, 0, 1 if down else -1, 0, False, False, False))


async def _table(app: App) -> StableTable:
    view = StableTable()
    await app.mount(view)
    view.add_column("Text")
    for i in range(80):
        view.add_row(str(i), key=str(i))
    return view


def test_wheel_events_scroll_once_for_the_batch():
    async def check():
        app = App()
        async with app.run_test(size=(40, 15)) as pilot:
            view = await _table(app)
            await pilot.pause()
            moves = []
            view.watch(view, "scroll_y", lambda y: moves.append(y), init=False)
            for _ in range(5):
                _wheel(view, down=True)
            _wheel(view, down=False)
            await pilot.pause()
            assert view.scroll_y == 0  # drawn when the timer fires, not per event
            await pilot.pause(WHEEL_SECONDS * 3)
            assert view.scroll_y == 4 * app.scroll_sensitivity_y
            assert moves == [view.scroll_y]

    asyncio.run(check())


@pytest.mark.parametrize("end", ["bottom", "top"])
def test_a_reversal_at_an_end_lands_where_unbatched_wheels_would(end):
    async def check():
        app = App()
        async with app.run_test(size=(40, 15)) as pilot:
            view = await _table(app)
            await pilot.pause()
            step = app.scroll_sensitivity_y
            start = view.max_scroll_y - 1 if end == "bottom" else 1
            view.scroll_to(y=start, animate=False, immediate=True)
            await pilot.pause()
            _wheel(view, down=end == "bottom")  # clamped to the end, one line
            _wheel(view, down=end != "bottom")  # then a full step back
            await pilot.pause(WHEEL_SECONDS * 3)
            expected = view.max_scroll_y - step if end == "bottom" else step
            assert view.scroll_y == expected

    asyncio.run(check())


def test_wheels_at_the_end_wait_for_the_batch():
    async def check():
        app = App()
        async with app.run_test(size=(40, 15)) as pilot:
            view = await _table(app)
            await pilot.pause()
            start = view.max_scroll_y - 5
            view.scroll_to(y=start, animate=False, immediate=True)
            await pilot.pause()
            moves = []
            view.watch(view, "scroll_y", lambda y: moves.append(y), init=False)
            for _ in range(5):
                _wheel(view, down=True)
            await pilot.pause()
            assert view.scroll_y == start  # Textual's own handler did not scroll ahead
            await pilot.pause(WHEEL_SECONDS * 3)
            assert moves == [view.max_scroll_y]

    asyncio.run(check())


def test_wheel_past_the_end_is_left_to_the_parent():
    seen = []

    class Parent(App):
        def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
            seen.append(event)

    async def check():
        app = Parent()
        async with app.run_test(size=(40, 15)) as pilot:
            view = await _table(app)
            await pilot.pause()
            _wheel(view, down=False)
            await pilot.pause(WHEEL_SECONDS * 3)
            assert view.scroll_y == 0
            assert view._wheel_timer is None
            assert len(seen) == 1

    asyncio.run(check())
