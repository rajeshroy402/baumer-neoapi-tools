import sys
import neoapi

try:
    cam = neoapi.Cam()
    cam.Connect()                      # first camera found
    img = cam.GetImage()
    if img.IsEmpty():
        sys.exit("empty frame")
    arr = img.GetNPArray()
    print("pixel format:", cam.f.PixelFormat.GetString())
    print("frame:", img.GetWidth(), "x", img.GetHeight(), "| array:", arr.shape, arr.dtype)
except neoapi.NeoException as e:
    sys.exit(f"neoAPI error: {e}")
