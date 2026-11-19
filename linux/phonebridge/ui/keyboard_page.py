"""Live typing into the phone: a focusable bar that sends every key straight into the
text box selected on the phone (as mirror.input text/key, executed by the phone's
accessibility service — see protocol/PROTOCOL.md section 4). Used by the Screen mirror tab.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

TRANSCRIPT_CHARS = 120


class LiveTypeBar(Gtk.Box):
    def __init__(self, on_input) -> None:
        super().__init__(spacing=10, focusable=True, css_classes=["type-pad"])
        self._on_input = on_input
        self._typed = ""

        icon = Gtk.Image.new_from_icon_name("input-keyboard-symbolic")
        self.append(icon)
        self._label = Gtk.Label(xalign=0, hexpand=True, ellipsize=1, css_classes=["type-transcript"])
        self.append(self._label)
        self._render(focused=False)

        # An input-method context makes accented/composed characters and non-Latin
        # layouts arrive as finished text.
        self._im = Gtk.IMMulticontext()
        self._im.connect("commit", lambda _im, text: self._send_text(text))
        keys = Gtk.EventControllerKey()
        keys.set_im_context(self._im)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda _c: (self._im.focus_in(), self._render(focused=True)))
        focus.connect("leave", lambda _c: (self._im.focus_out(), self._render(focused=False)))
        self.add_controller(focus)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_a: self.grab_focus())
        self.add_controller(click)

    def _render(self, focused: bool) -> None:
        if focused:
            self.add_css_class("focused")
        else:
            self.remove_css_class("focused")
        if self._typed:
            shown = self._typed.replace("\n", " ⏎ ")[-TRANSCRIPT_CHARS:]
            self._label.set_label(shown + ("▏" if focused else ""))
            self._label.remove_css_class("dim-label")
        else:
            self._label.set_label("Typing live to the phone…" if focused else
                                  "Click here and type — every key goes live into the phone's selected text box")
            self._label.add_css_class("dim-label")

    def _on_key(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False  # leave shortcuts to the PC
        if keyval == Gdk.KEY_BackSpace:
            self._on_input({"kind": "key", "key": "backspace"})
            self._typed = self._typed[:-1]
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._on_input({"kind": "key", "key": "enter"})
            self._typed += "\n"
        elif keyval == Gdk.KEY_Escape:
            self._on_input({"kind": "key", "key": "back"})
        else:
            return False  # printable keys arrive through the IM context's "commit"
        self._render(focused=True)
        return True

    def _send_text(self, text: str) -> None:
        if text:
            self._on_input({"kind": "text", "text": text})
            self._typed = (self._typed + text)[-1000:]
            self._render(focused=True)

    def type_into_phone(self, text: str) -> None:
        """Types a whole string (e.g. the PC clipboard) into the phone's selected box."""
        self._send_text(text)
