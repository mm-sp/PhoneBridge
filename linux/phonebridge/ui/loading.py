"""Progress bar shown while a tab waits for data from the phone.

The phone reports `load.progress {what, done, total}` while it reads its databases (see
PROTOCOL.md section 4b); main_window routes those, plus the request/response pair for
each `what`, to the owning page's LoadingMixin methods.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

NO_ANSWER_SECONDS = 60

# what -> (label, unit counted in `done/total`)
LABELS = {
    "sms.threads": ("Loading conversations", "messages"),
    "sms.thread": ("Loading messages", "messages"),
    "calls.history": ("Loading call history", "calls"),
    "contacts": ("Loading contacts", "numbers"),
    "files": ("Loading folder", "items"),
    "photos": ("Loading photos", "photos"),
    "apps": ("Loading apps", "apps"),
}


class LoadingBar(Gtk.Revealer):
    def __init__(self, what: str, **kwargs) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, **kwargs)
        self._what = what
        self._timeout_id = 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6)
        self._label = Gtk.Label(xalign=0, css_classes=["caption"])
        self._bar = Gtk.ProgressBar()
        box.append(self._label)
        box.append(self._bar)
        self.set_child(box)

    def start(self) -> None:
        self._label.set_label(f"{LABELS[self._what][0]}… waiting for phone")
        self._bar.set_fraction(0.0)
        self._label.remove_css_class("error")
        self.set_reveal_child(True)
        self._arm_timeout()

    def progress(self, done: int, total: int) -> None:
        if not self.get_reveal_child():
            self.set_reveal_child(True)
        title, unit = LABELS[self._what]
        frac = min(1.0, done / total) if total else 1.0
        self._bar.set_fraction(frac)
        self._label.set_label(f"{title}… {int(frac * 100)}%  ({done:,} of {total:,} {unit})")
        self._arm_timeout()

    def done(self) -> None:
        self._bar.set_fraction(1.0)
        self._cancel_timeout()
        GLib.timeout_add(250, lambda: (self.set_reveal_child(False), False)[1])

    def _arm_timeout(self) -> None:
        self._cancel_timeout()
        self._timeout_id = GLib.timeout_add_seconds(NO_ANSWER_SECONDS, self._on_timeout)

    def _cancel_timeout(self) -> None:
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0

    def _on_timeout(self) -> bool:
        self._timeout_id = 0
        self._label.set_label("The phone didn't answer — check it's connected, then press refresh.")
        self._label.add_css_class("error")
        return False


class LoadingMixin:
    """Pages keep `self._loading_bars: dict[what, LoadingBar]`."""

    _loading_bars: dict[str, LoadingBar]

    def loading_started(self, what: str) -> None:
        bar = self._loading_bars.get(what)
        if bar is not None:
            bar.start()

    def loading_progress(self, what: str, done: int, total: int) -> None:
        bar = self._loading_bars.get(what)
        if bar is not None:
            bar.progress(done, total)

    def loading_done(self, what: str) -> None:
        bar = self._loading_bars.get(what)
        if bar is not None:
            bar.done()
