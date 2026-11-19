from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .loading import LoadingBar, LoadingMixin
from .notifications_page import friendly_time

_CALL_ICONS = {
    "incoming": ("call-incoming-symbolic", "Incoming"),
    "outgoing": ("call-outgoing-symbolic", "Outgoing"),
    "missed": ("call-missed-symbolic", "Missed"),
    "rejected": ("call-missed-symbolic", "Declined"),
    "blocked": ("action-unavailable-symbolic", "Blocked"),
    "voicemail": ("audio-volume-high-symbolic", "Voicemail"),
}


def _duration(seconds: int) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class CallsPage(Gtk.Box, LoadingMixin):
    """Live call control plus keypad, call history and contacts — all dialled through the
    phone. Call audio stays on the phone (or its connected headset): Android doesn't let
    third-party apps route cellular call audio to another device."""

    def __init__(self, on_action, on_dial, on_message, on_request_history, on_request_contacts) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_action = on_action
        self._on_dial = on_dial
        self._on_message = on_message
        self._on_request_history = on_request_history
        self._on_request_contacts = on_request_contacts
        self._muted = False
        self._speaker = False
        self._contacts: list[dict] = []
        self._loaded = False
        self._call_started: float | None = None
        self._n_calls = 0
        self._n_missed = 0
        self._loading_bars = {
            "calls.history": LoadingBar("calls.history", margin_start=16, margin_end=16),
            "contacts": LoadingBar("contacts", margin_start=16, margin_end=16),
        }

        # --- active call banner ---
        self._call_box = Gtk.Box(spacing=12, margin_top=12, margin_start=16, margin_end=16, visible=False,
                                 css_classes=["card", "call-banner"])
        icon = Gtk.Image.new_from_icon_name("call-start-symbolic")
        icon.set_pixel_size(32)
        icon.set_margin_start(12)
        self._call_box.append(icon)
        call_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, margin_top=10, margin_bottom=10)
        self._call_title = Gtk.Label(xalign=0, css_classes=["title-3"])
        self._call_sub = Gtk.Label(xalign=0, css_classes=["dim-label"])
        call_text.append(self._call_title)
        call_text.append(self._call_sub)
        self._call_box.append(call_text)
        self._answer_btn = self._pill("Answer", "suggested-action", lambda: self._on_action("answer"))
        self._decline_btn = self._pill("Decline", "destructive-action", lambda: self._on_action("decline"))
        self._end_btn = self._pill("End call", "destructive-action", lambda: self._on_action("end"))
        self._mute_btn = self._pill("Mute", None, self._toggle_mute)
        self._speaker_btn = self._pill("Speaker", None, self._toggle_speaker)
        self._pc_audio_btn = Gtk.ToggleButton(label="Use PC audio", valign=Gtk.Align.CENTER, visible=False,
                                              tooltip_text="Hear and speak the call through this PC (Bluetooth "
                                                           "headset profile; the phone must be paired over Bluetooth)")
        self._pc_audio_btn.connect("toggled", self._on_pc_audio_toggled)
        self._pc_audio = False
        for b in (self._pc_audio_btn, self._mute_btn, self._speaker_btn, self._answer_btn, self._decline_btn,
                  self._end_btn):
            self._call_box.append(b)
        self._call_box.set_margin_end(16)
        self.append(self._call_box)

        # --- tabs ---
        self._stack = Adw.ViewStack(vexpand=True)
        self._stack.add_titled_with_icon(self._build_keypad(), "keypad", "Keypad", "input-dialpad-symbolic")
        self._stack.add_titled_with_icon(self._build_recent(), "recent", "Recent", "document-open-recent-symbolic")
        self._stack.add_titled_with_icon(self._build_contacts(), "contacts", "Contacts", "contact-new-symbolic")
        self._stack.set_visible_child_name("recent")
        switcher = Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE,
                                    halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=6)
        self.append(switcher)
        self.append(self._stack)
        GLib.timeout_add_seconds(1, self._tick)

    # ---------------------------------------------------------------- builders

    def _pill(self, label: str, css: str | None, cb) -> Gtk.Button:
        b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=["pill"] + ([css] if css else []))
        b.connect("clicked", lambda _b: cb())
        return b

    def _build_keypad(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                      valign=Gtk.Align.CENTER, margin_top=12)
        self._number = Gtk.Entry(placeholder_text="Enter a number", xalign=0.5, width_chars=18,
                                 css_classes=["title-2"], input_purpose=Gtk.InputPurpose.PHONE)
        self._number.connect("activate", lambda _e: self._dial(self._number.get_text()))
        box.append(self._number)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8, halign=Gtk.Align.CENTER)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
        for i, k in enumerate(keys):
            b = Gtk.Button(label=k, width_request=72, height_request=56, css_classes=["title-3", "circular"])
            b.connect("clicked", lambda _b, d=k: self._number.set_text(self._number.get_text() + d))
            grid.attach(b, i % 3, i // 3, 1, 1)
        box.append(grid)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        call = Gtk.Button(label="Call", css_classes=["suggested-action", "pill"], width_request=140)
        call.connect("clicked", lambda _b: self._dial(self._number.get_text()))
        sms = Gtk.Button(icon_name="chat-symbolic", tooltip_text="Send a text", css_classes=["circular"])
        sms.connect("clicked", lambda _b: self._number.get_text().strip() and self._on_message(self._number.get_text().strip(), ""))
        back = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Delete", css_classes=["circular"])
        back.connect("clicked", lambda _b: self._number.set_text(self._number.get_text()[:-1]))
        row.append(sms)
        row.append(call)
        row.append(back)
        box.append(row)
        return box

    def _build_recent(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tools = Gtk.Box(spacing=6, margin_start=16, margin_end=16, margin_bottom=6)
        self._recent_count = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label"])
        tools.append(self._recent_count)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda _b: self._on_request_history(0))
        tools.append(refresh)
        outer.append(tools)
        outer.append(self._loading_bars["calls.history"])
        self._recent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._recent_list.set_placeholder(Gtk.Label(label="No call history loaded", margin_top=24, margin_bottom=24,
                                                    css_classes=["dim-label"]))
        self._more_calls_btn = Gtk.Button(label="Load more calls", halign=Gtk.Align.CENTER, visible=False,
                                          css_classes=["pill"], margin_top=10)
        self._more_calls_btn.connect("clicked", lambda b: (b.set_sensitive(False), self._on_request_history(self._n_calls)))
        recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        recent_box.append(self._recent_list)
        recent_box.append(self._more_calls_btn)
        clamp = Adw.Clamp(maximum_size=760, margin_start=16, margin_end=16, margin_bottom=16)
        clamp.set_child(recent_box)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(clamp)
        outer.append(scroll)
        return outer

    def _build_contacts(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tools = Gtk.Box(spacing=6, margin_start=16, margin_end=16, margin_bottom=6)
        self._contact_search = Gtk.SearchEntry(placeholder_text="Search contacts", hexpand=True)
        self._contact_search.connect("search-changed", lambda _e: self._contact_list.invalidate_filter())
        tools.append(self._contact_search)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda _b: self._on_request_contacts(0))
        tools.append(refresh)
        outer.append(tools)
        outer.append(self._loading_bars["contacts"])
        self._contact_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._contact_list.set_filter_func(self._filter_contact)
        self._contact_list.set_placeholder(Gtk.Label(label="No contacts loaded", margin_top=24, margin_bottom=24,
                                                     css_classes=["dim-label"]))
        self._more_contacts_btn = Gtk.Button(label="Load more contacts", halign=Gtk.Align.CENTER, visible=False,
                                             css_classes=["pill"], margin_top=10)
        self._more_contacts_btn.connect(
            "clicked", lambda b: (b.set_sensitive(False), self._on_request_contacts(len(self._contacts))))
        contacts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        contacts_box.append(self._contact_list)
        contacts_box.append(self._more_contacts_btn)
        clamp = Adw.Clamp(maximum_size=760, margin_start=16, margin_end=16, margin_bottom=16)
        clamp.set_child(contacts_box)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(clamp)
        outer.append(scroll)
        return outer

    def _number_actions(self, row: Adw.ActionRow, number: str, name: str) -> None:
        msg = Gtk.Button(icon_name="chat-symbolic", tooltip_text="Text", valign=Gtk.Align.CENTER,
                         css_classes=["flat"])
        msg.connect("clicked", lambda _b: self._on_message(number, name))
        call = Gtk.Button(icon_name="call-start-symbolic", tooltip_text=f"Call {number}", valign=Gtk.Align.CENTER,
                          css_classes=["flat"])
        call.connect("clicked", lambda _b: self._dial(number))
        row.add_suffix(msg)
        row.add_suffix(call)

    # ---------------------------------------------------------------- data

    def on_shown(self) -> None:
        if not self._loaded:
            self._on_request_history(0)
            self._on_request_contacts(0)

    def reset(self) -> None:
        self._loaded = False

    def mark_requested(self) -> None:
        self._loaded = True

    def set_history(self, payload: dict) -> None:
        """A page of the call log: offset 0 replaces, later pages append."""
        self._loaded = True
        calls = payload.get("calls", [])
        if not payload.get("offset"):
            while (child := self._recent_list.get_first_child()) is not None:
                self._recent_list.remove(child)
            self._n_calls = 0
            self._n_missed = 0
        self._n_calls += len(calls)
        missed = 0
        for c in calls:
            kind = c.get("type", "")
            icon, label = _CALL_ICONS.get(kind, ("call-start-symbolic", "Call"))
            if kind == "missed":
                missed += 1
            name = c.get("name") or ""
            number = c.get("number") or "Private number"
            sub = " · ".join(x for x in (label, name and number, friendly_time(c.get("ts")), _duration(c.get("duration", 0))) if x)
            row = Adw.ActionRow(title=GLib.markup_escape_text(name or number), subtitle=GLib.markup_escape_text(sub))
            img = Gtk.Image.new_from_icon_name(icon)
            if kind in ("missed", "rejected"):
                img.add_css_class("error")
            row.add_prefix(img)
            if c.get("number"):
                self._number_actions(row, c["number"], name)
            self._recent_list.append(row)
        self._n_missed += missed
        total = payload.get("total")
        shown = f"{self._n_calls} of {total} calls" if total else f"{self._n_calls} calls"
        self._recent_count.set_label(shown + (f" · {self._n_missed} missed" if self._n_missed else ""))
        self._more_calls_btn.set_visible(bool(payload.get("has_more")))
        self._more_calls_btn.set_sensitive(True)

    def set_contacts(self, payload: dict) -> None:
        """A page of contacts (A-Z): offset 0 replaces, later pages append."""
        contacts = payload.get("contacts", [])
        if not payload.get("offset"):
            self._contacts = []
            while (child := self._contact_list.get_first_child()) is not None:
                self._contact_list.remove(child)
        self._contacts = self._contacts + contacts
        self._more_contacts_btn.set_visible(bool(payload.get("has_more")))
        self._more_contacts_btn.set_sensitive(True)
        total = payload.get("total")
        self._contact_search.set_placeholder_text(
            f"Search {len(self._contacts)} of {total} loaded contacts" if total else "Search contacts")
        for c in contacts:
            numbers = c.get("numbers") or []
            name = c.get("name") or (numbers[0]["number"] if numbers else "?")
            if len(numbers) <= 1:
                n = numbers[0] if numbers else {"number": "", "label": ""}
                row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                    subtitle=GLib.markup_escape_text(" · ".join(x for x in (n.get("label"), n["number"]) if x)))
                if n["number"]:
                    self._number_actions(row, n["number"], name)
            else:
                row = Adw.ExpanderRow(title=GLib.markup_escape_text(name), subtitle=f"{len(numbers)} numbers")
                for n in numbers:
                    sub = Adw.ActionRow(title=GLib.markup_escape_text(n["number"]),
                                        subtitle=GLib.markup_escape_text(n.get("label", "")))
                    self._number_actions(sub, n["number"], name)
                    row.add_row(sub)
            if c.get("starred"):
                row.add_prefix(Gtk.Image.new_from_icon_name("starred-symbolic"))
            row._search_text = (name + " " + " ".join(n["number"] for n in numbers)).lower()
            self._contact_list.append(row)

    def _filter_contact(self, row: Gtk.ListBoxRow) -> bool:
        q = self._contact_search.get_text().strip().lower()
        if not q:
            return True
        text = getattr(row, "_search_text", "")
        digits = "".join(ch for ch in q if ch.isdigit())
        return q in text or (len(digits) >= 3 and digits in "".join(ch for ch in text if ch.isdigit()))

    def _dial(self, number: str) -> None:
        number = number.strip()
        if number:
            self._on_dial(number)
            self._call_box.set_visible(True)
            self._call_title.set_label(f"Calling {self._name_for(number) or number}…")
            self._call_sub.set_label("Talk on your phone (or its headset)")
            self._set_buttons(ringing=False, active=True)

    def _name_for(self, number: str) -> str:
        digits = "".join(ch for ch in number if ch.isdigit())[-10:]
        for c in self._contacts:
            for n in c.get("numbers") or []:
                if digits and "".join(ch for ch in n["number"] if ch.isdigit())[-10:] == digits:
                    return c.get("name", "")
        return ""

    # ---------------------------------------------------------------- live call

    def on_ringing(self, caller_name: str, number: str) -> None:
        self._call_box.set_visible(True)
        who = caller_name or self._name_for(number) or number or "Unknown caller"
        self._call_title.set_label(f"Incoming call: {who}")
        self._call_sub.set_label(number if who != number else "")
        self._call_started = None
        self._set_buttons(ringing=True, active=False)

    def on_state(self, payload: dict) -> None:
        state = payload.get("state", "")
        number = payload.get("number", "")
        if state == "active":
            who = payload.get("caller_name") or self._name_for(number) or number or "Call"
            self._call_box.set_visible(True)
            self._call_title.set_label(who)
            self._call_started = time.time()
            self._set_buttons(ringing=False, active=True)
        elif state == "ended":
            self._call_box.set_visible(False)
            self._call_started = None
            if self._pc_audio:
                self._pc_audio_btn.set_active(False)  # restores the phone's normal Bluetooth profile
            self._muted = False
            self._speaker = False
            self._mute_btn.set_label("Mute")
            self._speaker_btn.set_label("Speaker")

    def _phone_name(self) -> str:
        root = self.get_root()
        try:
            return root._phone_name(root._fingerprint())
        except Exception:  # noqa: BLE001
            return ""

    def _on_pc_audio_toggled(self, button: Gtk.ToggleButton) -> None:
        import threading

        from .. import call_audio

        want, name = button.get_active(), self._phone_name()
        if want == self._pc_audio or not name:
            return
        self._pc_audio = want

        def work() -> None:
            if want:
                ok, message = call_audio.route_to_pc(name)
                if not ok:
                    GLib.idle_add(self._pc_audio_failed, message)
            else:
                call_audio.restore(name)

        threading.Thread(target=work, daemon=True).start()

    def _pc_audio_failed(self, message: str) -> bool:
        self._pc_audio = False
        self._pc_audio_btn.set_active(False)
        root = self.get_root()
        if hasattr(root, "toast"):
            root.toast(message)
        return False

    def _tick(self) -> bool:
        if self._call_started is not None:
            where = "audio on this PC" if self._pc_audio else "audio on phone"
            self._call_sub.set_label(f"In call · {_duration(int(time.time() - self._call_started)) or '0:00'}"
                                     f" · {where}")
        return True

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._on_action("mute" if self._muted else "unmute")
        self._mute_btn.set_label("Unmute" if self._muted else "Mute")

    def _toggle_speaker(self) -> None:
        self._speaker = not self._speaker
        self._on_action("speaker_on" if self._speaker else "speaker_off")
        self._speaker_btn.set_label("Speaker off" if self._speaker else "Speaker")

    def _set_buttons(self, ringing: bool, active: bool) -> None:
        self._answer_btn.set_visible(ringing)
        self._decline_btn.set_visible(ringing)
        self._end_btn.set_visible(active)
        self._mute_btn.set_visible(active)
        self._pc_audio_btn.set_visible(active)
        self._speaker_btn.set_visible(active)
