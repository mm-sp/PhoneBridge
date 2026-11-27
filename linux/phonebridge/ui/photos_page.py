from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, Gtk  # noqa: E402

from .loading import LoadingBar, LoadingMixin
from .notifications_page import friendly_time, texture_from_b64

PHOTOS_DIR = Path.home() / "Pictures" / "PhoneBridge"
PAGE_SIZE = 50


class PhotosPage(Gtk.Box, LoadingMixin):
    """Every photo on the phone as a thumbnail grid (loaded a page at a time, newest
    first). Click one to download it full-size into ~/Pictures/PhoneBridge and open it."""

    def __init__(self, on_request_list, on_request_photo) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_request_list = on_request_list
        self._on_request_photo = on_request_photo
        self._loaded = 0
        self._total = 0
        self._loading = False
        self._open_when_saved: set[str] = set()

        header = Gtk.Box(spacing=8, margin_top=12, margin_start=16, margin_end=16)
        self._count = Gtk.Label(label="Photos", xalign=0, hexpand=True, css_classes=["title-3"])
        header.append(self._count)
        folder = Gtk.Button(label="Open folder", tooltip_text=str(PHOTOS_DIR))
        folder.connect("clicked", lambda _b: self._open_path(PHOTOS_DIR, create=True))
        header.append(folder)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload from phone")
        refresh.connect("clicked", lambda _b: self.reload())
        header.append(refresh)
        self.append(header)
        self._loading_bars = {"photos": LoadingBar("photos", margin_start=16, margin_end=16)}
        self.append(self._loading_bars["photos"])

        self._flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=3,
                                 max_children_per_line=10, column_spacing=6, row_spacing=6,
                                 margin_start=16, margin_end=16, valign=Gtk.Align.START)
        self._scroll = Gtk.ScrolledWindow(vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._flow)
        self._more_btn = Gtk.Button(label=f"Load {PAGE_SIZE} more photos", halign=Gtk.Align.CENTER, visible=False, margin_bottom=16,
                                    css_classes=["pill"])
        self._more_btn.connect("clicked", lambda _b: self._load_next())
        box.append(self._more_btn)
        self._spinner = Gtk.Spinner(spinning=True, visible=False, margin_bottom=16)
        box.append(self._spinner)
        self._scroll.set_child(box)
        self.append(self._scroll)

        self._status_row = Gtk.Label(label="", css_classes=["caption", "dim-label"], margin_bottom=8)
        self.append(self._status_row)

    def on_shown(self) -> None:
        if self._loaded == 0 and not self._loading:
            self.reload()

    def reset(self) -> None:
        """The phone (re)connected: a page that was still loading on the old link will never arrive."""
        self._loaded = 0
        self._total = 0
        self._loading = False
        self._spinner.set_visible(False)
        self._open_when_saved.clear()

    def forget_phone(self) -> None:
        """Another phone takes over: nothing of the previous one's stays."""
        self.reset()
        self._clear_tiles()
        self._count.set_label("Photos")
        self._more_btn.set_visible(False)
        self._status_row.set_label("")

    def _clear_tiles(self) -> None:
        while (child := self._flow.get_first_child()) is not None:
            self._flow.remove(child)

    def reload(self) -> None:
        self._clear_tiles()
        self._loaded = 0
        self._total = 0
        self._loading = False
        self._load_next()

    def _load_next(self) -> None:
        if self._loading or (self._total and self._loaded >= self._total):
            return
        self._loading = True
        self._spinner.set_visible(True)
        self._more_btn.set_visible(False)
        self._on_request_list(self._loaded, PAGE_SIZE)

    def after_refusal(self, _what: str) -> None:
        self._loading = False
        self._spinner.set_visible(False)
        self._more_btn.set_visible(0 < self._loaded < self._total)

    def download_refused(self, message: str) -> None:
        self._open_when_saved.clear()
        self._status_row.set_label(message)

    def download_failed(self, photo_id: str, reason: str) -> None:
        self._open_when_saved.discard(photo_id)
        self._status_row.set_label(f"Download failed: {reason}")

    def set_photos(self, payload: dict) -> None:
        if payload.get("offset", 0) != self._loaded:
            return  # stale page (e.g. after a reload)
        self._loading = False
        self._spinner.set_visible(False)
        self._total = int(payload.get("total") or 0)
        for photo in payload.get("photos", []):
            self._flow.append(self._tile(photo))
            self._loaded += 1
        self._count.set_label(f"Photos · {self._loaded} of {self._total}" if self._total else "Photos")
        self._more_btn.set_visible(self._loaded < self._total)
        if not self._total:
            self._status_row.set_label("No photos found (allow the Storage permission in PhoneBridge on the phone)")

    def _tile(self, photo: dict) -> Gtk.Widget:
        photo_id = str(photo.get("id"))
        texture = texture_from_b64(photo.get("thumb", ""))
        picture = Gtk.Picture.new_for_paintable(texture) if texture else Gtk.Picture()
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(140, 140)
        button = Gtk.Button(css_classes=["flat", "photo-tile"],
                            tooltip_text=f"{photo.get('name', '')}\n{friendly_time(photo.get('taken_ts'))}")
        button.set_child(picture)
        button.connect("clicked", lambda _b: self._on_photo_clicked(photo_id, photo.get("name", "")))
        return button

    def _on_photo_clicked(self, photo_id: str, name: str) -> None:
        self._status_row.set_label(f"Downloading {name or photo_id}…")
        self._open_when_saved.add(photo_id)
        self._on_request_photo(photo_id)

    def on_photo_saved(self, path: Path, photo_id: str | None = None) -> None:
        self._status_row.set_label(f"Saved to {path}")
        if photo_id in self._open_when_saved:
            self._open_when_saved.discard(photo_id)
            # The file name comes from the phone: only images are opened automatically (never HTML/SVG/scripts).
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"):
                self._open_path(path)
            else:
                self._status_row.set_label(f"Saved to {path} (not opened automatically)")

    @staticmethod
    def _open_path(path: Path, create: bool = False) -> None:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
