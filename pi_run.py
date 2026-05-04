import cv2
import time
import struct
import socket
import argparse
import threading
import contextlib
import numpy as np
from picamera2 import Picamera2

import yaml
import logging
import logging.config

with open("logger.yaml") as f:
    logging.config.dictConfig(yaml.safe_load(f))
logger = logging.getLogger("server")

# Local Imports --------
from common_utils.wrapper import pprint

# ── Camera ─────────────────────────────────────────────────────────────────────

class CameraSource:
    """ Create the source and logistics of the PI Video Camera """

    def __init__(self, index: int = 0, width: int = 640, height: int = 480):

        try:
            Picamera2.global_camera_info()
        except Exception:
            pass

        # Launch and configure the video camera selected
        self._cam = Picamera2(index)
        self._cam.configure(self._cam.create_video_configuration(
            main={
                "format": "RGB888",
                "size": (width, height)
            }
        ))
        self._cam.start()
        time.sleep(0.5)
        pprint(f"Camera {index} opened ({width}x{height})")

    @property
    def size(self) -> tuple[int, int]:
        return tuple(self._cam.camera_configuration()["main"]["size"])

    def read_frame(self) -> np.ndarray | None:
        return self._cam.capture_array()

    def __enter__(self): return self
    def __exit__(self, *_):
        with contextlib.suppress(Exception):
            self._cam.stop()
        with contextlib.suppress(Exception):
            self._cam.close()

# ── Core loops ─────────────────────────────────────────────────────────────────

def video_loop(
    video_sock: socket.socket,
    ctrl_sock: socket.socket,
    cam: CameraSource,
    scale: float
) -> None:

    """ Rapidly capture the video frames and send it to each listener """

    w, h = cam.size
    video_sock.sendall(struct.pack(">IIII", w, h, 1, 1))
    pprint(f"Streaming {w}x{h} (scale {scale})")

    while True:
        frame = cam.read_frame()
        if frame is None:
            time.sleep(0.05)
            continue

        # Scale the video image based on given input ------
        if scale < 1.0:
            frame = cv2.resize(frame, (0, 0), fx=scale, fy=scale)
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            continue

        try: # Send to anyone that is listening
            video_sock.sendall(struct.pack(">I", len(data)) + data.tobytes())
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

# ── Server bootstrap ───────────────────────────────────────────────────────────

def create_server(port: int, name: str) -> socket.socket:
    """ Create the select server to run """

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen()
    pprint(f"{name} listening on :{port}")
    return s

def run(cam_index: int, scale: float, width: int, height: int) -> None:
    """ Start running the newly created server with desired settings """

    while True:
        video_srv = create_server(3000, "Video")

        try:
            video_sock, addr = video_srv.accept()
            pprint(f"Client connected: {addr}")
        finally:
            video_srv.close()

        with contextlib.suppress(Exception):
            with CameraSource(cam_index, width, height) as cam:
                video_loop(video_sock, cam, scale)

        pprint("Client disconnected — restarting…")
        with contextlib.suppress(Exception):
            video_sock.close()
        time.sleep(1)

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pi camera streaming server")
    parser.add_argument("--cam",    default=0,    type=int,   help="Camera index (default: 0)")
    parser.add_argument("--scale",  default=1.0,  type=float, help="Downsample factor (default: 1.0)")
    parser.add_argument("--width",  default=640,  type=int,   help="Capture width (default: 640)")
    parser.add_argument("--height", default=480,  type=int,   help="Capture height (default: 480)")
    args = parser.parse_args()
    pprint(f"Camera: {args.cam} | {args.width}x{args.height} | Scale: {args.scale}")

    thread = threading.Thread(target=run, args=(args.cam, args.scale, args.width, args.height), daemon=True)
    thread.start()
    thread.join()

if __name__ == "__main__":
    main()
