#!/usr/bin/env python3
"""Capture test: Baumer neoAPI grab + OpenCV display/save (no DeepStream).

  python capture_cv.py                         # live window, first camera found
  python capture_cv.py --serial 12345678       # specific camera
  python capture_cv.py --exposure-us 8000      # set exposure (microseconds)
  python capture_cv.py --no-display --frames 100 --save-dir ./frames --save-every 25
  python capture_cv.py --synthetic             # no camera: tests the OpenCV side only

Window keys: q / ESC (or closing the window) = quit, s = save current frame.
"""
import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np


class SyntheticSource:
    """Moving colour-gradient test pattern, for checking OpenCV with no camera."""

    def __init__(self, width=1920, height=1080, fps=30.0):
        self.period = 1.0 / fps
        self.n = 0
        ramp = np.linspace(0, 255, width, dtype=np.uint8)
        self.base = np.tile(ramp, (height, 1))

    def read(self):
        time.sleep(self.period)
        g = np.roll(self.base, (self.n * 4) % self.base.shape[1], axis=1)
        frame = np.dstack([g, np.roll(g, 85, axis=1), np.roll(g, 170, axis=1)])
        self.n += 1
        return np.ascontiguousarray(frame)


class NeoSource:
    """Baumer camera via neoAPI; neoAPI converts the raw/Bayer frame to BGR8."""

    def __init__(self, serial=None, exposure_us=None):
        import neoapi

        self.cam = neoapi.Cam()
        if serial:
            self.cam.Connect(serial)
        else:
            self.cam.Connect()
        if exposure_us is not None:
            self.cam.f.ExposureAuto.SetString("Off")  # ExposureTime is read-only while auto is on
            self.cam.f.ExposureTime.Set(exposure_us)
        print("camera pixel format:", self.cam.f.PixelFormat.GetString())

    def read(self):
        img = self.cam.GetImage()
        if img.IsEmpty():
            return None
        bgr = img.Convert("BGR8")
        arr = bgr.GetNPArray()
        if arr.ndim == 2 and arr.shape[1] == bgr.GetWidth() * 3:
            arr = arr.reshape(bgr.GetHeight(), bgr.GetWidth(), 3)
        return arr.copy()


def save_frame(frame, save_dir, n):
    path = Path(save_dir) / f"frame_{n:06d}.png"
    cv2.imwrite(str(path), frame)
    print("saved", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="camera serial number (default: first camera found)")
    ap.add_argument("--exposure-us", type=float,
                    help="exposure time in microseconds (turns ExposureAuto off until changed back)")
    ap.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = run until quit)")
    ap.add_argument("--no-display", action="store_true", help="do not open a window")
    ap.add_argument("--save-dir", default=".", help="where saved frames go (default: current dir)")
    ap.add_argument("--save-every", type=int, default=0, help="save every Nth frame (0 = only on 's' key)")
    ap.add_argument("--synthetic", action="store_true", help="use a test pattern instead of the camera")
    args = ap.parse_args()

    Path(args.save_dir).mkdir(parents=True, exist_ok=True)

    try:
        src = SyntheticSource() if args.synthetic else NeoSource(args.serial, args.exposure_us)
    except Exception as e:  # neoapi.NeoException, ImportError, ...
        sys.exit(f"could not open source: {e}")

    show = not args.no_display
    n = 0
    n_report = 0
    misses = 0
    t_report = time.time()

    try:
        while args.frames == 0 or n < args.frames:
            frame = src.read()
            if frame is None:
                misses += 1
                if misses >= 50:
                    sys.exit("50 consecutive empty frames, giving up")
                continue
            misses = 0
            n += 1

            now = time.time()
            if now - t_report >= 1.0:
                fps = (n - n_report) / (now - t_report)
                print(f"frame {n}: {frame.shape[1]}x{frame.shape[0]} @ {fps:.1f} fps")
                n_report, t_report = n, now

            if args.save_every and n % args.save_every == 0:
                save_frame(frame, args.save_dir, n)

            if show:
                h, w = frame.shape[:2]
                disp = frame if w <= 1280 else cv2.resize(frame, (1280, int(h * 1280 / w)))
                disp = disp.copy()
                cv2.putText(disp, f"#{n}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                try:
                    cv2.imshow("capture", disp)
                    key = cv2.waitKey(1) & 0xFF
                except cv2.error as e:
                    sys.exit(f"cannot open a window ({e}); rerun with --no-display")
                if key in (27, ord("q")) or cv2.getWindowProperty("capture", cv2.WND_PROP_VISIBLE) < 1:
                    break   # q / ESC, or the window was closed
                if key == ord("s"):
                    save_frame(frame, args.save_dir, n)
    except KeyboardInterrupt:
        pass
    finally:
        if show:
            cv2.destroyAllWindows()
    print(f"done: {n} frames")


if __name__ == "__main__":
    main()
