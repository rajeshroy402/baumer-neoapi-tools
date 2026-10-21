#!/usr/bin/env python3
"""Baumer camera GUI: live view plus every camera feature neoAPI exposes.

  python camera_gui.py                        # first camera found
  python camera_gui.py --serial 700011878780  # a specific camera

The feature panel is built from the camera itself and grouped by the camera's own
GenICam categories (the same layout as Camera Explorer), so it adapts to any model.
Names are the ones you use in code, e.g. cam.f.ExposureTime / "BayerRG8".
Greyed-out features are read-only or locked by another setting (for example
ExposureTime while ExposureAuto is not Off). Hover a feature for its description.

View: mouse wheel = zoom, drag = pan, double-click = fit.
Keys: Space = pause/resume, Ctrl+S = snapshot, F = fit, 1 = 100 %, F5 = re-read all.
"""
import argparse
import collections
import contextlib
import hashlib
import html
import io
import itertools
import math
import re
import signal
import socket
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import neoapi

# PySide6 6.4 (the newest that runs without the libxcb-cursor0 system package) was
# built against NumPy 1.x. Under NumPy 2 its import prints a long warning and turns
# off its NumPy interop, which this script doesn't use, so hide the noise.
with contextlib.redirect_stderr(io.StringIO()):
    from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt
CACHE_DIR = Path.home() / ".cache" / "neoapi_gui"
SNAP_DIR = Path(__file__).resolve().parent / "snapshots"
LEVELS = ["Beginner", "Expert", "Guru"]
ROW_HEIGHT = 24
TEXT_INT_REPR = ("HexNumber", "IPV4Address", "MACAddress")
# Writes that are hard to undo or can make the camera unreachable ask first.
CONFIRM = {
    "DeviceReset": "Reboot the camera? The connection drops and this app has to be restarted.",
    "DeviceResetToDeliveryState": "Reset the camera to its factory state? All saved user sets are erased.",
    "UserSetSave": "Overwrite the selected user set stored in the camera?",
    "boCalibrationDataSave": "Overwrite the calibration data stored in the camera?",
}
IP_PREFIXES = ("GevPersistent", "GevCurrentIPConfiguration")
IP_WARNING = "Changing the camera's IP configuration can make it unreachable from this PC. Continue?"


def first_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


# ---------------------------------------------------------------- camera helpers

def read_genicam_xml(cam):
    """The camera's GenICam XML, read via the GigE Vision bootstrap 'First URL' register (cached)."""
    def read(addr, n):
        buf = bytearray(n)
        cam.ReadMemory(addr, buf, n)
        return bytes(buf)

    url = read(0x200, 512).split(b"\0")[0].decode(errors="replace")
    cache = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".xml")
    if cache.exists():
        return cache.read_bytes()
    m = re.match(r"(?i)local:(?:///)?([^;]+);([0-9a-f]+);([0-9a-f]+)", url)
    if not m:
        raise ValueError(f"unsupported XML location {url!r}")
    name, addr, length = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
    data = b"".join(read(addr + off, min(512, length - off)) for off in range(0, length, 512))
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(z.namelist()[0])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def category_tree(xml_bytes, names):
    """Category nodes (name, display name, children) under Root; children are nodes or feature names."""
    root = ET.fromstring(xml_bytes)
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    cats = {
        c.get("Name"): (c.findtext(ns + "DisplayName") or c.get("Name"),
                        [p.text.strip() for p in c.findall(ns + "pFeature") if p.text])
        for c in root.iter(ns + "Category")
    }

    def build(name):
        display, kids = cats[name]
        out = []
        for k in kids:
            if k in cats:
                sub = build(k)
                if sub[2]:
                    out.append(sub)
            elif k in names:
                out.append(k)
        return name, display, out

    return build("Root")[2]


def placed_names(nodes):
    for n in nodes:
        if isinstance(n, str):
            yield n
        else:
            yield from placed_names(n[2])


def parse_int(text, rep):
    t = text.strip()
    if rep == "IPV4Address":
        return int.from_bytes(socket.inet_aton(t), "big")
    if rep == "MACAddress":
        return int(re.sub(r"[:\-]", "", t), 16)
    return int(t, 0)


def read_state(f, iface):
    """Snapshot of one feature: availability, value, range / enum entries."""
    s = {"avail": f.IsAvailable()}
    if not s["avail"] or iface in ("ICommand", "IRegister"):
        return s
    if not f.IsReadable():
        s["readable"] = False
        return s
    try:
        if iface == "IBoolean":
            s["value"] = f.GetBool()
        elif iface == "IEnumeration":
            s["value"] = f.GetString()
            s["entries"] = [e.GetName() for e in f.GetEnumValueList() if e.IsAvailable()]
        elif iface == "IInteger":
            s.update(value=f.GetInt(), text=f.GetString(), min=f.GetIntMin(), max=f.GetIntMax(), inc=f.GetIntInc())
        elif iface == "IFloat":
            s.update(value=f.GetDouble(), min=f.GetDoubleMin(), max=f.GetDoubleMax())
        else:
            s["text"] = f.GetString()
    except Exception as e:
        s["error"] = first_line(e)
    return s


def to_qimage(img):
    """Convert a neoAPI image (any pixel format) to an 8-bit QImage that owns its data."""
    pf = img.GetPixelFormat()
    mono = pf.startswith("Mono")
    target = "Mono8" if mono else "RGB8"
    if pf != target:
        img = img.Convert(target)
    arr = np.ascontiguousarray(img.GetNPArray())
    h, w = arr.shape[:2]
    fmt = QtGui.QImage.Format.Format_Grayscale8 if mono else QtGui.QImage.Format.Format_RGB888
    return QtGui.QImage(arr.data, w, h, arr.strides[0], fmt).copy()


# ---------------------------------------------------------------- worker threads

class Grabber(QtCore.QThread):
    """Owns the stream: grabs frames, pauses/resumes, and holds the stream stopped on request."""

    frame_ready = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self, cam):
        super().__init__()
        self.cam = cam
        self.running = True
        self.paused = False
        self.count = 0                      # frames grabbed so far
        self._latest = None                 # (QImage, pixel format) of the newest frame
        self._lock = threading.Lock()
        self._hold = threading.Event()      # another thread wants the stream stopped
        self._held = threading.Event()      # ... and now it is
        self._release = threading.Event()

    def hold(self, timeout=3.0):
        """Stop the stream until release(). Called from another thread."""
        self._release.clear()
        self._held.clear()
        self._hold.set()
        return self._held.wait(timeout)

    def release(self):
        self._hold.clear()
        self._release.set()

    def take(self):
        with self._lock:
            latest, self._latest = self._latest, None
        return latest

    def run(self):
        streaming = self.cam.IsStreaming()
        last_error = None
        while self.running:
            try:
                if self._hold.is_set():
                    if streaming:
                        self.cam.StopStreaming()
                        streaming = False
                    self._held.set()
                    self._release.wait(5.0)
                    continue
                if self.paused == streaming:
                    if self.paused:
                        self.cam.StopStreaming()
                    else:
                        self.cam.StartStreaming()
                    streaming = not self.paused
                if not streaming:
                    self.msleep(30)
                    continue
                img = self.cam.GetImage(300)
                if img.IsEmpty():
                    continue
                frame = (to_qimage(img), img.GetPixelFormat())
            except Exception as e:
                msg = first_line(e)
                if msg != last_error:
                    self.failed.emit(msg)
                last_error = msg
                self.msleep(300)
                continue
            last_error = None
            with self._lock:
                self._latest = frame
            self.count += 1
            self.frame_ready.emit()


class FeatureWorker(QtCore.QThread):
    """All feature reads and writes, off the GUI thread.

    Some reads take 20-50 ms each, and IsWritable() costs ~100 ms per feature while
    streaming but ~0 with the stream stopped, so write access is scanned in one pass
    with the stream briefly held (~0.25 s). Writes go before reads, and repeated
    writes to one feature (slider drags) collapse to the newest value.
    """

    states = QtCore.Signal(list)            # [(name, state dict), ...]
    access = QtCore.Signal(dict)            # {name: writable}
    wrote = QtCore.Signal(str, str, bool)   # name, message, ok

    def __init__(self, features, ifaces, grabber):
        super().__init__()
        self.features, self.ifaces, self.grabber = features, ifaces, grabber
        self.running = True
        self._cv = threading.Condition()
        self._writes = collections.OrderedDict()   # key -> (name, kind, value)
        self._reads = collections.OrderedDict()    # ordered set of names
        self._scan = False
        self._ids = itertools.count()

    def write(self, name, kind, value=None):
        key = (name, next(self._ids)) if kind == "exec" else name
        with self._cv:
            self._writes.pop(key, None)
            self._writes[key] = (name, kind, value)
            self._cv.notify()

    def read(self, names):
        with self._cv:
            for n in names:
                self._reads[n] = None
            self._cv.notify()

    def scan_access(self):
        with self._cv:
            self._scan = True
            self._cv.notify()

    def busy(self):
        with self._cv:
            return bool(self._writes or self._reads or self._scan)

    def stop(self):
        with self._cv:
            self.running = False
            self._cv.notify()

    def run(self):
        batch = []
        while True:
            with self._cv:
                if not (batch and not self._reads):
                    while self.running and not (self._writes or self._reads or self._scan):
                        self._cv.wait()
                if not self.running:
                    return
                if self._writes:
                    job, arg = "write", self._writes.popitem(last=False)[1]
                elif self._scan:
                    job, arg = "scan", None
                    self._scan = False
                elif self._reads:
                    job, arg = "read", self._reads.popitem(last=False)[0]
                else:
                    job, arg = None, None
            if batch and (job != "read" or len(batch) >= 8):
                self.states.emit(batch)
                batch = []
            if job == "write":
                self._write(*arg)
            elif job == "scan":
                self._scan_access()
            elif job == "read":
                batch.append((arg, read_state(self.features[arg], self.ifaces[arg])))

    def _write(self, name, kind, value):
        f = self.features[name]
        try:
            if kind == "exec":
                f.Execute()
                msg = f"{name} executed"
            else:
                {"bool": f.SetBool, "int": f.SetInt, "float": f.SetDouble, "str": f.SetString}[kind](value)
                msg = f"{name} = {value}"
            self.wrote.emit(name, msg, True)
        except Exception as e:
            self.wrote.emit(name, f"{name}: {first_line(e)}", False)

    def _scan_access(self):
        self.grabber.hold()
        try:
            acc = {}
            for n, f in self.features.items():
                try:
                    acc[n] = f.IsWritable()
                except Exception:
                    acc[n] = False
        finally:
            self.grabber.release()
        self.access.emit(acc)


# ---------------------------------------------------------------- widgets

class ImageView(QtWidgets.QWidget):
    """Shows the newest frame: wheel zooms about the cursor, drag pans, double-click fits."""

    hover = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.img = None
        self.fit = True
        self.scale, self.ox, self.oy = 1.0, 0.0, 0.0
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)

    def set_image(self, img):
        self.img = img
        self.update()

    def set_fit(self):
        self.fit = True
        self.update()

    def set_actual(self):
        if self.img is None:
            return
        self._zoom_at(QtCore.QPointF(self.width() / 2, self.height() / 2), 1.0 / self.scale)

    def _layout(self):
        if self.fit and self.img is not None:
            iw, ih = self.img.width(), self.img.height()
            self.scale = min(self.width() / iw, self.height() / ih)
            self.ox = (self.width() - iw * self.scale) / 2
            self.oy = (self.height() - ih * self.scale) / 2

    def _zoom_at(self, pos, k):
        self._layout()
        k = max(0.02, min(50.0, self.scale * k)) / self.scale
        self.fit = False
        self.ox = pos.x() - (pos.x() - self.ox) * k
        self.oy = pos.y() - (pos.y() - self.oy) * k
        self.scale *= k
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(20, 20, 22))
        if self.img is None:
            p.setPen(QtGui.QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "waiting for frames…")
            return
        self._layout()
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, self.scale < 1)
        p.drawImage(QtCore.QRectF(self.ox, self.oy, self.img.width() * self.scale,
                                  self.img.height() * self.scale), self.img)

    def wheelEvent(self, ev):
        if self.img is not None:
            self._zoom_at(ev.position(), 1.25 ** (ev.angleDelta().y() / 120))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.position()

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        self.set_fit()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._drag is not None:
            self._layout()
            d = pos - self._drag
            self._drag = pos
            self.fit = False
            self.ox += d.x()
            self.oy += d.y()
            self.update()
        if self.img is None:
            return
        x, y = int((pos.x() - self.ox) / self.scale), int((pos.y() - self.oy) / self.scale)
        if 0 <= x < self.img.width() and 0 <= y < self.img.height():
            c = self.img.pixelColor(x, y)
            gray = self.img.format() == QtGui.QImage.Format.Format_Grayscale8
            val = f"I={c.red()}" if gray else f"R={c.red()} G={c.green()} B={c.blue()}"
            self.hover.emit(f"x={x} y={y}  {val}")
        else:
            self.hover.emit("")


class WheelGuard(QtCore.QObject):
    """Wheel over an unfocused editor scrolls the panel instead of changing a camera setting."""

    def __init__(self, viewport):
        super().__init__(viewport)
        self.viewport = viewport

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Wheel and not obj.hasFocus():
            QtCore.QCoreApplication.sendEvent(self.viewport, ev)
            return True
        return False


class NumberEditor(QtWidgets.QWidget):
    """Slider + spin box for an IInteger/IFloat feature; the slider is log-scaled for wide ranges."""

    STEPS = 1000

    def __init__(self, row, precision):
        super().__init__()
        self.row = row
        self.is_int = row.iface == "IInteger"
        self.lo, self.hi, self.inc = 0.0, 0.0, 0   # hi <= lo: range not read yet
        self.log = False
        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.STEPS)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setKeyboardTracking(False)
        self.spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin.setDecimals(0 if self.is_int else max(0, min(precision, 4)))
        if not self.is_int:
            self.spin.setStepType(QtWidgets.QAbstractSpinBox.StepType.AdaptiveDecimalStepType)
        if row.unit:
            self.spin.setSuffix(" " + row.unit)
        self.spin.setMinimumWidth(120)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.spin.valueChanged.connect(self._spin_changed)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderReleased.connect(row.panel.schedule_refresh)

    def editing(self):
        return self.slider.isSliderDown() or self.spin.hasFocus()

    def set_range(self, lo, hi, inc):
        if (lo, hi, inc) == (self.lo, self.hi, self.inc):
            return
        self.lo, self.hi, self.inc = lo, hi, inc
        self.log = lo > 0 and hi / lo >= 1000
        self.spin.blockSignals(True)
        self.spin.setRange(lo, hi)
        if self.is_int:
            self.spin.setSingleStep(max(1, inc or 1))
        self.spin.blockSignals(False)

    def show_value(self, v):
        for w, val in ((self.spin, v), (self.slider, self._to_slider(v))):
            w.blockSignals(True)
            w.setValue(val)
            w.blockSignals(False)

    def set_writable(self, on):
        self.spin.setEnabled(on)
        self.slider.setVisible(on and self.hi > self.lo)

    def _to_slider(self, v):
        if self.hi <= self.lo:
            return 0
        if self.log:
            t = math.log(max(v, self.lo) / self.lo) / math.log(self.hi / self.lo)
        else:
            t = (v - self.lo) / (self.hi - self.lo)
        return round(max(0.0, min(1.0, t)) * self.STEPS)

    def _from_slider(self, s):
        t = s / self.STEPS
        v = self.lo * (self.hi / self.lo) ** t if self.log else self.lo + t * (self.hi - self.lo)
        if self.is_int:
            inc = max(1, self.inc or 1)
            v = int(min(self.hi, max(self.lo, self.lo + round((v - self.lo) / inc) * inc)))
        return v

    def _slider_changed(self, s):
        v = self._from_slider(s)
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self._send(v)

    def _spin_changed(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(v))
        self.slider.blockSignals(False)
        self._send(v)

    def _send(self, v):
        if self.is_int:
            self.row.panel.write(self.row, "int", int(round(v)))
        else:
            self.row.panel.write(self.row, "float", float(v))


class FeatureRow:
    """One feature in the tree: an editor that mirrors the camera value and queues writes."""

    def __init__(self, panel, feat, item):
        self.panel, self.item = panel, item
        self.name = feat.GetName()
        self.iface = feat.GetInterface()
        numeric = self.iface in ("IInteger", "IFloat")
        self.rep = feat.GetRepresentation() if numeric else ""
        self.unit = feat.GetUnit() if numeric else ""
        self.label = feat.GetDisplayName() or self.name
        vis = feat.GetVisibility()
        self.level = LEVELS.index(vis) if vis in LEVELS else len(LEVELS)
        self.available = True
        self.writable = self.iface == "ICommand"
        self.state = {}
        self._entries = None

        desc = html.escape(feat.GetDescription().strip()).replace("\n", "<br>")
        tip = (f"<b>{html.escape(self.name)}</b> &nbsp;<i>{html.escape(self.label)}</i><br>"
               f"{self.iface[1:]} · {vis}{' · ' + html.escape(self.unit) if self.unit else ''}<br><br>{desc}")
        item.setText(0, self.name)
        item.setToolTip(0, tip)
        item.setData(0, Qt.ItemDataRole.UserRole, self.name)
        self.editor = self._make_editor(feat)
        self.editor.setToolTip(tip)
        self.editor.setMinimumHeight(ROW_HEIGHT)
        panel.tree.setItemWidget(item, 1, self.editor)
        self._render()

    def _make_editor(self, feat):
        guard = self.panel.wheel_guard
        if self.iface == "IBoolean":
            w = QtWidgets.QCheckBox()
            w.clicked.connect(lambda on: self.panel.write(self, "bool", bool(on)))
        elif self.iface == "IEnumeration":
            w = QtWidgets.QComboBox()
            w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            w.installEventFilter(guard)
            w.activated.connect(lambda i: self.panel.write(self, "str", w.itemText(i)))
        elif self.iface == "ICommand":
            w = QtWidgets.QPushButton("Execute")
            w.clicked.connect(lambda: self.panel.write(self, "exec"))
        elif self.iface in ("IInteger", "IFloat") and self.rep not in TEXT_INT_REPR:
            prec = feat.GetDoublePrecision() if self.iface == "IFloat" else 0
            w = NumberEditor(self, prec)
            w.spin.installEventFilter(guard)
            w.slider.installEventFilter(guard)
        elif self.iface == "IRegister":
            w = QtWidgets.QLabel("binary register")
        else:
            w = QtWidgets.QLineEdit()
            w.editingFinished.connect(lambda: self._text_done(w.text()))
        return w

    def editing(self):
        if isinstance(self.editor, NumberEditor):
            return self.editor.editing()
        return isinstance(self.editor, QtWidgets.QLineEdit) and self.editor.hasFocus()

    def apply(self, state):
        self.state = state
        self.available = state.get("avail", True)
        self._render()

    def set_writable(self, on):
        # Only the enabled state: self.state may be older than a write still being read back.
        self.writable = on or self.iface == "ICommand"
        self._render_access()

    def _render_access(self):
        s, w = self.state, self.editor
        on = self.writable and s.get("readable", True) and "error" not in s
        if isinstance(w, NumberEditor):
            w.set_writable(on)
        elif not isinstance(w, QtWidgets.QLabel):
            w.setEnabled(on)

    def _render(self):
        self._render_access()
        s, w = self.state, self.editor
        if self.editing() or ("value" not in s and "text" not in s):
            return
        if isinstance(w, NumberEditor):
            if "min" in s:
                w.set_range(s["min"], s["max"], s.get("inc", 0))
            w.show_value(s["value"])
        elif isinstance(w, QtWidgets.QCheckBox):
            w.blockSignals(True)
            w.setChecked(bool(s["value"]))
            w.blockSignals(False)
        elif isinstance(w, QtWidgets.QComboBox):
            w.blockSignals(True)
            entries = s.get("entries", [])
            if s["value"] not in entries:
                entries = entries + [s["value"]]
            if entries != self._entries:
                w.clear()
                w.addItems(entries)
                self._entries = entries
            w.setCurrentIndex(entries.index(s["value"]))
            w.blockSignals(False)
        elif isinstance(w, QtWidgets.QLineEdit):
            w.setText(s.get("text", ""))
            w.setCursorPosition(0)

    def _text_done(self, text):
        if not self.writable or text == self.state.get("text"):
            return
        if self.iface == "IString":
            self.panel.write(self, "str", text)
            return
        try:
            v = parse_int(text, self.rep)
        except (ValueError, OSError):
            self.panel.message.emit(f"{self.name}: can't parse {text!r}", True)
            self._render()
            return
        self.panel.write(self, "int", v)


class FeaturePanel(QtWidgets.QWidget):
    """Searchable tree of every feature, grouped by the camera's categories."""

    message = QtCore.Signal(str, bool)   # text, is_error

    def __init__(self, features, worker, categories):
        super().__init__()
        self.worker = worker
        self.rows = {}
        self._confirming = False

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Filter features…")
        self.search.setClearButtonEnabled(True)
        self.level = QtWidgets.QComboBox()
        self.level.addItems(LEVELS)
        self.level.setCurrentText("Guru")
        self.level.setToolTip("Feature visibility level: Beginner shows the essentials, Guru shows everything")
        self.live = QtWidgets.QCheckBox("Live values")
        self.live.setChecked(True)
        self.live.setToolTip("Re-read the features on screen once a second")

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Feature", "Value"])
        self.tree.header().resizeSection(0, 240)
        self.tree.setAlternatingRowColors(True)
        self.tree.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.wheel_guard = WheelGuard(self.tree.viewport())

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.level)
        top.addWidget(self.live)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(top)
        lay.addWidget(self.tree)

        bold = self.font()
        bold.setBold(True)
        for node in categories:
            self._add(None, node, features, bold)

        self._refresh_timer = QtCore.QTimer(self, singleShot=True, interval=150)
        self._refresh_timer.timeout.connect(self.refresh_shown)
        self._live_timer = QtCore.QTimer(self, interval=1000)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_timer.start()

        self.tree.itemExpanded.connect(lambda it: self.worker.read(self._names_under(it)))
        self.tree.verticalScrollBar().valueChanged.connect(self.schedule_refresh)
        self.search.textChanged.connect(self._filter_changed)
        self.level.currentIndexChanged.connect(self._filter_changed)
        worker.states.connect(self._on_states)
        worker.access.connect(self._on_access)
        worker.wrote.connect(self._on_wrote)

    def _add(self, parent, node, features, bold):
        if isinstance(node, str):
            f = features[node]
            if f.GetVisibility() not in LEVELS:      # "Invisible": not meant for GUIs
                return
            item = QtWidgets.QTreeWidgetItem(parent) if parent else QtWidgets.QTreeWidgetItem(self.tree)
            self.rows[node] = FeatureRow(self, f, item)
            return
        _, display, children = node
        item = QtWidgets.QTreeWidgetItem(parent) if parent else QtWidgets.QTreeWidgetItem(self.tree)
        item.setText(0, display)
        item.setFont(0, bold)
        item.setSizeHint(0, QtCore.QSize(0, ROW_HEIGHT + 2))
        item.setFirstColumnSpanned(True)
        for child in children:
            self._add(item, child, features, bold)
        if item.childCount() == 0:
            (parent or self.tree.invisibleRootItem()).removeChild(item)

    def _names_under(self, item):
        out = []
        for i in range(item.childCount()):
            ch = item.child(i)
            name = ch.data(0, Qt.ItemDataRole.UserRole)
            out += [name] if name else self._names_under(ch)
        return out

    def shown_rows(self):
        """Rows currently on screen (all ancestors expanded and inside the viewport)."""
        view = self.tree.viewport().rect()
        out = []
        for r in self.rows.values():
            ok = not r.item.isHidden()
            p = r.item.parent()
            while ok and p is not None:
                ok = p.isExpanded() and not p.isHidden()
                p = p.parent()
            if ok and self.tree.visualItemRect(r.item).intersects(view):
                out.append(r)
        return out

    def schedule_refresh(self, *_):
        self._refresh_timer.start()

    def refresh_shown(self):
        self.worker.read([r.name for r in self.shown_rows()])

    def read_all(self):
        self.worker.scan_access()
        self.worker.read(list(self.rows))

    def _live_tick(self):
        if self.live.isChecked() and not self.worker.busy():
            self.refresh_shown()

    def _filter_changed(self, *_):
        self.apply_filter()
        self.schedule_refresh()

    def apply_filter(self):
        text = self.search.text().strip().lower()
        level = self.level.currentIndex()

        def visit(item):
            name = item.data(0, Qt.ItemDataRole.UserRole)
            if name:
                r = self.rows[name]
                vis = r.available and r.level <= level and (
                    not text or text in r.name.lower() or text in r.label.lower())
            else:
                vis = False
                for i in range(item.childCount()):
                    vis = visit(item.child(i)) or vis
                if text and vis and not item.isExpanded():
                    item.setExpanded(True)
            item.setHidden(not vis)
            return vis

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def write(self, row, kind, value=None):
        if self._confirming:
            return
        warning = CONFIRM.get(row.name) or (IP_WARNING if row.name.startswith(IP_PREFIXES) else None)
        if warning:
            self._confirming = True
            try:
                answer = QtWidgets.QMessageBox.question(self, row.name, warning)
            finally:
                self._confirming = False
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                self.worker.read([row.name])
                return
        self.worker.write(row.name, kind, value)

    def _on_states(self, batch):
        changed = False
        for name, s in batch:
            r = self.rows.get(name)
            if r is None:
                continue
            was = r.available
            r.apply(s)
            changed |= was != r.available
        if changed:
            self.apply_filter()

    def _on_access(self, acc):
        for name, w in acc.items():
            if name in self.rows:
                self.rows[name].set_writable(w)

    def _on_wrote(self, name, msg, ok):
        self.message.emit(msg, not ok)
        r = self.rows.get(name)
        if r is not None and r.iface in ("IEnumeration", "IBoolean", "ICommand"):
            self.worker.scan_access()       # modes and selectors can lock/unlock other features
        self.worker.read([name])            # the camera may have adjusted or rejected the value
        self.schedule_refresh()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, cam, features, categories):
        super().__init__()
        self.cam, self.features = cam, features

        def text(name):
            try:
                return features[name].GetString() if name in features else ""
            except Exception:
                return ""

        self.model, self.serial = text("DeviceModelName"), text("DeviceSerialNumber")
        self.setWindowTitle(f"{text('DeviceVendorName')} {self.model} — {self.serial}")

        ifaces = {n: f.GetInterface() for n, f in features.items()}
        self.grabber = Grabber(cam)
        self.worker = FeatureWorker(features, ifaces, self.grabber)
        self.view = ImageView()
        self.panel = FeaturePanel(features, self.worker, categories)

        split = QtWidgets.QSplitter()
        split.addWidget(self.view)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setSizes([1060, 540])
        self.setCentralWidget(split)

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        self.pause_act = tb.addAction("Pause")
        self.pause_act.setCheckable(True)
        self.pause_act.setShortcut(Qt.Key.Key_Space)
        self.pause_act.toggled.connect(self._pause)
        snap = tb.addAction("Snapshot")
        snap.setShortcut(QtGui.QKeySequence("Ctrl+S"))
        snap.triggered.connect(self.snapshot)
        if "TriggerSoftware" in features:
            trig = tb.addAction("Software trigger")
            trig.setToolTip("Execute TriggerSoftware (for TriggerMode = On, TriggerSource = Software)")
            trig.triggered.connect(lambda: self.worker.write("TriggerSoftware", "exec"))
        tb.addSeparator()
        fit = tb.addAction("Fit")
        fit.setShortcut(Qt.Key.Key_F)
        fit.triggered.connect(self.view.set_fit)
        actual = tb.addAction("100 %")
        actual.setShortcut(Qt.Key.Key_1)
        actual.triggered.connect(self.view.set_actual)
        tb.addSeparator()
        reread = tb.addAction("Re-read all")
        reread.setShortcut(Qt.Key.Key_F5)
        reread.setToolTip("Re-read every feature value and which ones are writable")
        reread.triggered.connect(self.panel.read_all)

        sb = self.statusBar()
        self.hover_label = QtWidgets.QLabel()
        self.frame_label = QtWidgets.QLabel()
        self.fps_label = QtWidgets.QLabel()
        for w in (self.hover_label, self.frame_label, self.fps_label):
            sb.addPermanentWidget(w)
        self.view.hover.connect(self.hover_label.setText)
        self.panel.message.connect(self.notify)
        self.grabber.frame_ready.connect(self._on_frame)
        self.grabber.failed.connect(lambda m: self.notify(f"acquisition: {m}", True))

        self.last_frame = None
        self._shown = 0
        self._fps_mark = (time.monotonic(), 0, 0)
        fps_timer = QtCore.QTimer(self, interval=1000)
        fps_timer.timeout.connect(self._update_fps)
        fps_timer.start()

        self.grabber.start()
        self.worker.start()
        self.panel.read_all()
        self.resize(1600, 950)

    def notify(self, msg, error=False):
        sb = self.statusBar()
        sb.setStyleSheet("color: #ff6b6b;" if error else "")
        sb.showMessage(msg, 8000 if error else 4000)

    def _on_frame(self):
        latest = self.grabber.take()
        if latest is None:
            return
        img, fmt = latest
        self.last_frame = img
        self.view.set_image(img)
        self._shown += 1
        self.frame_label.setText(f"{img.width()}×{img.height()} {fmt} · {self.view.scale * 100:.0f} %")

    def _update_fps(self):
        t0, g0, s0 = self._fps_mark
        now, g, s = time.monotonic(), self.grabber.count, self._shown
        dt = max(now - t0, 1e-6)
        if self.pause_act.isChecked():
            self.fps_label.setText("paused")
        else:
            self.fps_label.setText(f"camera {(g - g0) / dt:.1f} fps · shown {(s - s0) / dt:.0f}")
        self._fps_mark = (now, g, s)

    def _pause(self, on):
        self.grabber.paused = on
        self.pause_act.setText("Resume" if on else "Pause")

    def snapshot(self):
        if self.last_frame is None:
            self.notify("no frame yet", True)
            return
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        path = SNAP_DIR / f"{self.model}_{self.serial}_{stamp}.png"
        if self.last_frame.save(str(path)):
            self.notify(f"saved {path}")
        else:
            self.notify(f"could not save {path}", True)

    def closeEvent(self, ev):
        # Stop both threads before disconnecting: exiting while a thread is inside
        # GetImage() aborts the process.
        self.grabber.running = False
        self.worker.stop()
        self.grabber.release()
        self.worker.wait(5000)
        self.grabber.wait(5000)
        try:
            self.cam.Disconnect()
        except Exception:
            pass
        ev.accept()


# ---------------------------------------------------------------- startup

def apply_dark_theme(app):
    app.setStyle("Fusion")
    R, G, c = QtGui.QPalette.ColorRole, QtGui.QPalette.ColorGroup, QtGui.QColor
    p = QtGui.QPalette()
    for role, col in ((R.Window, c(45, 45, 48)), (R.WindowText, c(225, 225, 225)),
                      (R.Base, c(30, 30, 32)), (R.AlternateBase, c(38, 38, 41)),
                      (R.ToolTipBase, c(55, 55, 60)), (R.ToolTipText, c(230, 230, 230)),
                      (R.Text, c(225, 225, 225)), (R.Button, c(58, 58, 62)),
                      (R.ButtonText, c(225, 225, 225)), (R.Highlight, c(42, 130, 218)),
                      (R.HighlightedText, c(255, 255, 255)), (R.Link, c(90, 160, 255)),
                      (R.PlaceholderText, c(135, 135, 140))):
        p.setColor(role, col)
    for role in (R.Text, R.WindowText, R.ButtonText):
        p.setColor(G.Disabled, role, c(120, 120, 120))
    p.setColor(G.Disabled, R.Base, c(38, 38, 41))   # disabled check boxes stay visible
    app.setPalette(p)


def connect_camera(serial):
    while True:
        try:
            cam = neoapi.Cam()
            if serial:
                cam.Connect(serial)
            else:
                cam.Connect()
            return cam
        except Exception as e:
            msg = first_line(e)
            if "NoAccess" in type(e).__name__ or "used by someone else" in msg:
                msg += "\n\nIf Camera Explorer or another script has the camera open, disconnect it there and press Retry."
            buttons = QtWidgets.QMessageBox.StandardButton
            if QtWidgets.QMessageBox.critical(None, "Camera not available", msg,
                                              buttons.Retry | buttons.Cancel) != buttons.Retry:
                return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="camera serial number (default: first camera found)")
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv[:1])
    apply_dark_theme(app)
    cam = connect_camera(args.serial)
    if cam is None:
        return 1

    features = {f.GetName(): f for f in cam.GetFeatureList()}
    try:
        categories = category_tree(read_genicam_xml(cam), set(features))
    except Exception as e:
        print(f"note: camera categories unavailable ({first_line(e)}), showing a flat list", file=sys.stderr)
        categories = []
    rest = sorted(set(features) - set(placed_names(categories)))
    if rest:
        categories.append(("Other", "Other", rest))

    win = MainWindow(cam, features, categories)
    win.show()
    # Ctrl+C in the terminal closes the window cleanly; the timer lets Python see the signal.
    signal.signal(signal.SIGINT, lambda *_: win.close())
    tick = QtCore.QTimer(interval=200)
    tick.timeout.connect(lambda: None)
    tick.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
