from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .loading import LoadingBar, LoadingMixin
from .notifications_page import texture_from_b64


class AppsPage(Gtk.Box, LoadingMixin):
    """Every launchable app on the phone. Clicking one opens it on the phone ("Open on
    phone"), mirrors the phone with that app in front ("Mirror the phone"), or streams just
    that app from a private virtual display so the phone's own screen is left alone
    ("Stream only the app" — experimental: the app must allow being resized)."""

    OPEN, MIRROR, STREAM = range(3)

    def __init__(self, on_request_list, on_launch, on_launch_and_mirror, on_stream_app=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_request_list = on_request_list
        self._on_launch = on_launch
        self._on_launch_and_mirror = on_launch_and_mirror
        self._on_stream_app = on_stream_app
        self._labels: dict[str, str] = {}
        self._loaded = False

        header = Gtk.Box(spacing=8, margin_top=12, margin_start=16, margin_end=16)
        self._count = Gtk.Label(label="Apps", xalign=0, css_classes=["title-3"])
        header.append(self._count)
        self._search = Gtk.SearchEntry(placeholder_text="Search apps", hexpand=True)
        self._search.connect("search-changed", lambda _e: self._flow.invalidate_filter())
        header.append(self._search)
        modes = ["Open on phone", "Mirror the phone"] + (["Stream only the app"] if on_stream_app else [])
        self._mode = Gtk.DropDown.new_from_strings(modes)
        self._mode.set_selected(self.MIRROR)
        self._mode.set_tooltip_text("What clicking an app does. \"Stream only the app\" shows just that app on the "
                                    "PC, in the background, without taking over the phone's screen (experimental).")
        header.append(self._mode)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload app list")
        refresh_btn.connect("clicked", lambda _b: self._request())
        header.append(refresh_btn)
        self.append(header)
        self._loading_bars = {"apps": LoadingBar("apps", margin_start=16, margin_end=16)}
        self.append(self._loading_bars["apps"])

        self._flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=3,
                                 max_children_per_line=10, column_spacing=6, row_spacing=6,
                                 margin_start=16, margin_end=16, margin_bottom=16, valign=Gtk.Align.START)
        self._flow.set_filter_func(self._filter)
        self._stack = Gtk.Stack(vexpand=True)
        self._spinner_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=12)
        self._spinner_page.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        self._spinner_page.append(Gtk.Label(label="Loading apps from phone…", css_classes=["dim-label"]))
        self._stack.add_named(self._spinner_page, "loading")
        self._stack.add_named(Adw.StatusPage(title="No apps loaded", icon_name="view-app-grid-symbolic",
                                             description="Connect your phone, then press refresh."), "empty")
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self._flow)
        self._stack.add_named(scroll, "grid")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

    def on_shown(self) -> None:
        if not self._loaded:
            self._request()

    def reset(self) -> None:
        self._loaded = False

    def _request(self) -> None:
        self._on_request_list()

    def after_refusal(self, _what: str) -> None:
        self._loaded = False   # ask again the next time the tab is opened

    def forget_phone(self) -> None:
        """Another phone takes over: the previous one's apps go."""
        self._loaded = False
        self._clear_tiles()
        self._labels.clear()
        self._search.set_text("")
        self._count.set_label("Apps")
        self._stack.set_visible_child_name("empty")

    def _clear_tiles(self) -> None:
        while (child := self._flow.get_first_child()) is not None:
            self._flow.remove(child)

    def set_apps(self, apps: list[dict]) -> None:
        self._loaded = True
        self._clear_tiles()
        for app in apps:
            self._flow.append(self._tile(app))
        self._count.set_label(f"Apps · {len(apps)}")
        self._stack.set_visible_child_name("grid" if apps else "empty")

    def _tile(self, app: dict) -> Gtk.Widget:
        pkg = app.get("pkg", "")
        label_text = app.get("label", pkg)
        self._labels[pkg] = label_text
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6)
        texture = texture_from_b64(app.get("icon", ""))
        image = Gtk.Image.new_from_paintable(texture) if texture else Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        image.set_pixel_size(48)
        box.append(image)
        box.append(Gtk.Label(label=label_text, wrap=True, max_width_chars=12, lines=2, ellipsize=3,
                             justify=Gtk.Justification.CENTER))
        button = Gtk.Button(css_classes=["flat"], tooltip_text=f"{label_text}\n{pkg}")
        button.set_child(box)
        button.connect("clicked", lambda _b: self._launch(pkg))
        child = Gtk.FlowBoxChild()
        child.set_child(button)
        child._search_text = f"{label_text} {pkg}".lower()
        return child

    def _launch(self, pkg: str) -> None:
        mode = self._mode.get_selected()
        if mode == self.STREAM and self._on_stream_app:
            self._on_stream_app(pkg, self._labels.get(pkg, pkg))
        elif mode == self.MIRROR:
            self._on_launch_and_mirror(pkg)
        else:
            self._on_launch(pkg)

    def _filter(self, child: Gtk.FlowBoxChild) -> bool:
        q = self._search.get_text().strip().lower()
        return not q or q in getattr(child, "_search_text", "")
