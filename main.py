import mss
import cv2
import time
import queue
import socket
import struct
import ctypes
import threading
import contextlib
import numpy as np
from typing import List
from pynput.keyboard import Controller, Key

# Local Imports
from wrapper import pprint

# DEFINE CONSTANTS -------------
MOUSEEVENTF_MOVE        = 0x0001
MOUSEEVENTF_LEFTDOWN    = 0x0002
MOUSEEVENTF_LEFTUP      = 0x0004
MOUSEEVENTF_ABSOLUTE    = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

# Setup pynput config -------
_keyboard = Controller()
SPECIAL_KEYS = {
    'return':     Key.enter,
    'backspace': Key.backspace,
    'tab':       Key.tab,
    'escape':    Key.esc,
    'space':     Key.space,
    'left':      Key.left,
    'right':     Key.right,
    'up':        Key.up,
    'down':      Key.down,
    'delete':    Key.delete,
    'home':      Key.home,
    'end':       Key.end,
    'page up':   Key.page_up,
    'page down': Key.page_down,
    'shift':     Key.shift,
    'ctrl':      Key.ctrl,
    'alt':       Key.alt,
    'f1':        Key.f1,
    'f2':        Key.f2,
    'f3':        Key.f3,
    'f4':        Key.f4,
    'f5':        Key.f5,
}

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]

class Displays:
    """ Class to manage display information and operations """

    def __init__(self) -> None:
        self.monitor = mss.mss().monitors[0]

    def change_view(self, index: int) -> None:
        """ Change the current display to the specified index """

        num_displays = self.get_total_display()
        if index >= num_displays + 1:
            print(f"Invalid display index: {index}. Available displays: {num_displays}")
            return

        #TODO: Send updated screen capture -----------
        self.monitor = mss.mss().monitors[index]
        self.get_size()
        print(f"Switched to display {index}: {self.monitor}")

    def get_size(self) -> tuple:
        """ Get the screen size of monitor (width, height)"""

        width, height = self.monitor["width"], self.monitor["height"]
        pprint(f"Monitor Size: ({width}, {height})")
        return self.monitor["width"], self.monitor["height"]
    
    def get_total_display(self) -> int:
        """ Get the total number of displays """

        return len(mss.mss().monitors)

def key_action(key_queue:queue.Queue) -> None:
    """ Process all of the queued key presses coming in """

    while not key_queue.empty():
        try:
            # Check if it's a special key first
            code = key_queue.get()
            _keyboard.tap(code)
            print(f"Received key: {code}")
        except Exception as e:
            print(f"Failed to send key {code}: {e}")

def key_event(action_event):
    """ Action on what keys are pressed on the client side """

    key_press = action_event.split("key:")
    if not key_press:
        pprint("Unable to parse key press event...")
        return

    # Run through each key press action --------
    key_queue = queue.Queue()
    for k in key_press[1:]:
        special = SPECIAL_KEYS.get(k.lower())
        if special: k = special
        key_queue.put(k.strip(), block=True)
    key_action(key_queue)

def display_event(action_event, displays: Displays, control_conn:socket.socket) -> None:
    """ Get the action on what display is being viewed """

    _, display_idx = action_event.split("view:")
    if not display_idx:
        pprint("Unable to parse display switch event...")
        return

    display_idx = int(display_idx.strip())
    displays.change_view(display_idx)

    # Send new resolution back to client
    w, h = displays.get_size()
    msg = f"res:{w},{h}\n"
    control_conn.sendall(msg.encode('utf-8'))
    pprint(f"Sent new resolution: {w}x{h}")

def click_event(action_event, displays: Displays) -> None:
    """ Create the offset to apply to the select display based on what is sent """

    _, coords = action_event.split("click:")
    x_str, y_str = coords.strip().split(",")
    x, y = int(x_str), int(y_str)

    abs_x = displays.monitor["left"] + x
    abs_y = displays.monitor["top"]  + y

    virt_x = ctypes.windll.user32.GetSystemMetrics(76)
    virt_y = ctypes.windll.user32.GetSystemMetrics(77)
    virt_w = ctypes.windll.user32.GetSystemMetrics(78)
    virt_h = ctypes.windll.user32.GetSystemMetrics(79)

    norm_x = int((abs_x - virt_x) * 65535 / virt_w)
    norm_y = int((abs_y - virt_y) * 65535 / virt_h)

    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    move = INPUT(type=0, mi=MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=None))
    down = INPUT(type=0, mi=MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=flags | MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=None))
    up   = INPUT(type=0, mi=MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=flags | MOUSEEVENTF_LEFTUP,   time=0, dwExtraInfo=None))
    ctypes.windll.user32.SendInput(3, (INPUT * 3)(move, down, up), ctypes.sizeof(INPUT))
    pprint(f"Click ({x},{y}) -> abs ({abs_x},{abs_y}) -> norm ({norm_x},{norm_y})")

def send_handshake(tcp_connection: socket.socket, capture_w: int, capture_h: int, num_display: int) -> None:
    """Send screen dimensions once at the start"""

    tcp_connection.sendall(struct.pack('>III', capture_w, capture_h, num_display))
    pprint(f"Sent handshake: {capture_w}x{capture_h} - Total Displays: {num_display}")

def restart_stream(video_conn: socket.socket, control_conn: socket.socket,  displays: Displays) -> None:
    """Restart the streaming process after a client disconnects"""

    pprint("Client disconnected. Restarting stream...")
    with contextlib.suppress(Exception):
        video_conn.close()
        control_conn.close()
    time.sleep(1)
    start_stream(displays)

def control_handler(control_conn: socket.socket, displays: Displays) -> None:
    """Dedicated thread — reads and processes control events independently of video."""
    
    buffer = ""
    while True:
        try:
            control_conn.settimeout(1.0)
            chunk = control_conn.recv(1024).decode('utf-8')
            if not chunk:
                break
            buffer += chunk

            # Process all complete messages
            while '\n' in buffer:
                msg, buffer = buffer.split('\n', 1)
                msg = msg.strip()
                if not msg: continue

                # Event Actions --------------
                if msg.startswith("key:"):
                    key_event(msg)
                elif msg.startswith("click:"):
                    click_event(msg, displays)
                elif msg.startswith("view:"):
                    display_event(msg, displays, control_conn)
        except socket.timeout:
            continue
        except (BrokenPipeError, ConnectionResetError, OSError):
            pprint("Error: closing control thread...")
            break

def start_video(video_conn: socket.socket, control_conn: socket.socket, displays: Displays) -> None:
    """Start the video streaming loop — no longer touches control socket."""

    # Spin up control handler in its own thread
    ctrl_thread = threading.Thread(
        target=control_handler,
        args=(control_conn, displays),
        daemon=True
    )
    ctrl_thread.start()

    with mss.mss() as sct:
        capture_w, capture_h = displays.get_size()
        num_displays = displays.get_total_display()
        send_handshake(video_conn, capture_w, capture_h, num_displays)

        while True:
            display = sct.grab(displays.monitor)
            img = np.asarray(display)[:, :, :3]
            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 60]
            result, frame_bytes = cv2.imencode('.jpg', frame, encode_param)
            if not result:
                continue

            try: # Send the screenshot frames to streamer
                video_conn.settimeout(None)
                video_conn.sendall(struct.pack('>I', len(frame_bytes)) + frame_bytes.tobytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                restart_stream(video_conn, control_conn, displays)
                return

def create_tcp_server(host:str='0.0.0.0', name="", port:int=3000) -> socket.socket:
    """Create and return a TCP server socket bound to the specified host and port."""

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen()
    pprint(f"TCP {name} server created and listening on {port}")
    return server_socket

def start_stream(displays: Displays, timeout=True) -> int:
    """ Start the streaming server and handle incoming client connections """

    try:
        video_server = create_tcp_server(port=3000, name="Video")
        control_server = create_tcp_server(port=3001, name="Controller")

        if timeout: # Check if server will timeout or not
            pprint("Server will timeout in an hour...")
            video_server.settimeout(6000)
            control_server.settimeout(6000)

        # Wait to connect then close ------------
        video_conn, addr = video_server.accept()
        control_conn, _ = control_server.accept()
        video_server.close()
        control_server.close()

        pprint(f"Client connected from {addr}")
        start_video(video_conn, control_conn, displays)
        return 0
    except TimeoutError:
        pprint("No client connected within timeout.")
        return 1
    except OSError as e:
        pprint(f"Socket error in start_stream: {e}")
        return -1
    except Exception as e:
        pprint(f"Unexpected error in start_stream: {e}")
        return -1

def main(timeout:bool=True) -> None:
    """ Main entry point for the application """

    displays = Displays()
    video_thread = threading.Thread(
        target=start_stream,
        args=(displays, timeout),
        daemon=True
    )
    video_thread.start()
    _status = video_thread.join()
    pprint(f"Video thread exited with status: {_status}")

if __name__ == "__main__":
    timeout = False
    main(timeout)
