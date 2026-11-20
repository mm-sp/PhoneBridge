#!/usr/bin/env python3
"""Guards the Android manifest's orientation decisions (no Android SDK, device or display needed).

* MainActivity (the launcher screen) is landscape-only BY DESIGN: `sensorLandscape` = both landscape directions,
  chosen by the sensor. The tab column, terminal and mirror views are laid out for a wide screen.
* The QR scanner (zxing's CaptureActivity) deliberately stays `fullSensor` so a portrait phone can scan; the
  manifest merger needs `tools:replace="screenOrientation"` there.

The test also proves it is not vacuous: copies of the manifest with either value changed must be reported.
Run: `python3 tests/test_android_manifest.py`"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[2] / "android/app/src/main/AndroidManifest.xml"
ANDROID = "{http://schemas.android.com/apk/res/android}"
TOOLS = "{http://schemas.android.com/tools}"
QR_SCANNER = "com.journeyapps.barcodescanner.CaptureActivity"
MAIN_NAMES = (".MainActivity", "dev.phonebridge.MainActivity")

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def problems(xml_text: str) -> list[str]:
    """Everything wrong with the orientation setup of this manifest text (empty list = fine)."""
    out: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return [f"manifest is not valid XML: {exc}"]
    acts = root.findall("./application/activity")
    mains = [a for a in acts if a.get(ANDROID + "name") in MAIN_NAMES]
    if len(mains) != 1:
        out.append(f"expected exactly one MainActivity declaration, found {len(mains)}")
    else:
        main = mains[0]
        value = main.get(ANDROID + "screenOrientation")
        if value != "sensorLandscape":
            out.append(f"MainActivity screenOrientation is {value!r}, must be 'sensorLandscape'")
        if "orientation" not in (main.get(ANDROID + "configChanges") or "").split("|"):
            out.append("MainActivity must declare configChanges=orientation (it re-arranges itself, it is not recreated)")
        launcher = any(c.get(ANDROID + "name") == "android.intent.category.LAUNCHER" for c in main.iter("category"))
        if not launcher:
            out.append("MainActivity is no longer the launcher activity")
    qrs = [a for a in acts if a.get(ANDROID + "name") == QR_SCANNER]
    if len(qrs) != 1:
        out.append(f"expected exactly one QR scanner (CaptureActivity) declaration, found {len(qrs)}")
    else:
        qr = qrs[0]
        value = qr.get(ANDROID + "screenOrientation")
        if value != "fullSensor":
            out.append(f"QR scanner screenOrientation is {value!r}, must stay 'fullSensor'")
        if "screenOrientation" not in (qr.get(TOOLS + "replace") or ""):
            out.append("QR scanner must keep tools:replace=\"screenOrientation\" (needed to override the library's value)")
    return out


def main() -> int:
    text = MANIFEST.read_text(encoding="utf-8")
    print("1. the real manifest")
    found = problems(text)
    check("MainActivity is sensorLandscape, the QR scanner is fullSensor", not found)
    for p in found:
        print("       ", p)

    root = ET.fromstring(text)
    acts = {a.get(ANDROID + "name"): a for a in root.findall("./application/activity")}
    main_act = next((a for n, a in acts.items() if n in MAIN_NAMES), None)
    check("MainActivity value read directly: sensorLandscape",
          main_act is not None and main_act.get(ANDROID + "screenOrientation") == "sensorLandscape")
    check("QR scanner value read directly: fullSensor",
          acts.get(QR_SCANNER) is not None and acts[QR_SCANNER].get(ANDROID + "screenOrientation") == "fullSensor")

    print("2. the guard really fires (mutated copies must be reported)")
    main_line = 'android:screenOrientation="sensorLandscape"'
    qr_line = 'android:screenOrientation="fullSensor"'
    check("fixture lines exist in the manifest", main_line in text and qr_line in text)
    for bad in ("fullSensor", "portrait", "sensorPortrait", "landscape", "unspecified", "user"):
        check(f"MainActivity changed to {bad!r} is rejected",
              any("MainActivity screenOrientation" in p for p in problems(text.replace(main_line, f'android:screenOrientation="{bad}"'))))
    check("MainActivity with no orientation at all is rejected",
          any("MainActivity screenOrientation" in p for p in problems(text.replace(main_line, ""))))
    for bad in ("landscape", "portrait", "sensorLandscape", "unspecified"):
        check(f"QR scanner changed to {bad!r} is rejected",
              any("QR scanner screenOrientation" in p for p in problems(text.replace(qr_line, f'android:screenOrientation="{bad}"'))))
    check("QR scanner losing tools:replace is rejected",
          any("tools:replace" in p for p in problems(text.replace('tools:replace="screenOrientation"', ""))))
    check("MainActivity without configChanges=orientation is rejected",
          any("configChanges" in p for p in problems(text.replace("orientation|screenSize|keyboardHidden|screenLayout", "screenSize"))))
    check("a manifest without MainActivity is rejected",
          any("MainActivity declaration" in p for p in problems(text.replace('android:name=".MainActivity"', 'android:name=".Other"'))))
    check("an unparsable manifest is rejected", bool(problems("<manifest")))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
