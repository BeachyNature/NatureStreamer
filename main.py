import mss
import cv2
import time
import socket
import struct
import keyboard
import win32api
import win32con
import threading
import contextlib
import numpy as np
from typing import List

# Local Imports
from wrapper import pprint

class Displays:
    """ Class to manage display information and operations """

    def __init__(self):

        self.monitor = mss.mss().monitors[1]

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

def key_action(key_code:List[str]) -> None:
    """ Action on what keys are pressed on the client side """

    for code in key_code:
        code = code.strip()
        keyboard.press(code)
        time.sleep(0.005)
        keyboard.release(code)
        print(f"Recieved key: {code}")

def change_display(action_event, displays: Displays) -> None:
    """ Action on display switch event from client """

    _, display_idx= action_event.split("view:")
    if not display_idx:
        pprint("Unable to parse display switch event...")
        return

    # Change the display -----------
    display_idx = int(display_idx.strip())
    displays.change_view(display_idx)

def click_event(action_event):
    """ Action on left mouse click at the given screen coordinates """

    _, coords = action_event.split("click:")
    if not coords:
        pprint("Unable to parse click coordinates...")
        return
    
    coords = coords.strip()
    x_str, y_str = coords.split(",")
    x, y = int(x_str), int(y_str)
    pprint(f"Received click: ({x},{y})")

    win32api.SetCursorPos((x,y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,x,y,0,0)
    time.sleep(0.005)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,x,y,0,0)

def key_event(action_event):
    """ Action on what keys are pressed on the client side """

    key_press = action_event.split("key:")
    if not key_press:
        pprint("Unable to parse key press event...")
        return

    # Run through each key press action --------
    key_press = key_press[1:]
    key_action(key_press)

def send_handshake(tcp_connection: socket.socket, capture_w: int, capture_h: int, num_display: int) -> None:
    """Send screen dimensions once at the start"""

    tcp_connection.sendall(struct.pack('>III', capture_w, capture_h, num_display))
    pprint(f"Sent handshake: {capture_w}x{capture_h} - Total Displays: {num_display}")

def restart_stream(tcp_connection: socket.socket, displays: Displays) -> None:
    """Restart the streaming process after a client disconnects"""

    pprint("Client disconnected. Restarting stream...")
    with contextlib.suppress(Exception):
        tcp_connection.close()

    time.sleep(1) # Give enough time to release port -----
    start_stream(displays)

def start_video(tcp_connection: socket.socket, displays: Displays) -> None:
    """Start the video streaming loop"""
    
    with mss.mss() as sct:
        # Get information about the server -------
        capture_w, capture_h = displays.get_size()
        num_displays = displays.get_total_display()
        send_handshake(tcp_connection, capture_w, capture_h, num_displays)

        while True:
            display = sct.grab(displays.monitor)
            img = np.asarray(display)[:, :, :3]
            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 60]
            result, frame_bytes = cv2.imencode('.jpg', frame, encode_param)
            if not result:
                pprint("Failed to encode frame. Skipping.")
                continue

            frame_size = len(frame_bytes)
            struct_size = struct.pack('>I', frame_size)

            try:
                # Prevent timeout on sendall ---------------
                tcp_connection.settimeout(None) 
                tcp_connection.sendall(struct_size + frame_bytes.tobytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                restart_stream(tcp_connection, displays)
                return

            try:
                # Short timeout on action event -------
                tcp_connection.settimeout(0.005)
    
                # Check event queue ---------
                action_event = tcp_connection.recv(1024).decode('utf-8')
                if action_event.startswith("key:"):
                    key_event(action_event)
                elif action_event.startswith("click:"):
                    click_event(action_event)
                elif action_event.startswith("view:"):
                    change_display(action_event, displays)

            except (BlockingIOError, socket.timeout):
                continue

            except (BrokenPipeError, ConnectionResetError, OSError):
                restart_stream(tcp_connection, displays)
                return

            except Exception as e:
                pprint(f"Error during event handling: {e}")
                continue

def create_tcp_server() -> socket.socket:
    """Create and return a TCP server socket bound to the specified host and port."""

    host, port = '0.0.0.0', 3000
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen()
    server_socket.settimeout(6000) # Set timeout for an hour
    pprint(f"TCP server created and listening on {host}:{port}")
    return server_socket

def start_stream(displays: Displays) -> int:
    """ Start the streaming server and handle incoming client connections """

    try:
        server_socket = create_tcp_server()
        conn, addr = server_socket.accept()
        server_socket.close()
        pprint(f"Client connected from {addr}")
        start_video(conn, displays)
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

def main(keep: bool = True) -> None:
    """ Main entry point for the application """

    displays = Displays()
    video_thread = threading.Thread(
        target=start_stream,
        args=(displays,),
        daemon=True
    )
    video_thread.start()
    _status = video_thread.join()
    pprint(f"Video thread exited with status: {_status}")

if __name__ == "__main__":
    keep = True
    main(keep)
