import sys
import cv2
import time
import struct
import socket
import keyboard
import threading
import numpy as np
import win32api, win32con
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QImage, QAction
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow, QPushButton, QWidget, QLabel, 
    QPushButton, QComboBox, QLineEdit, QVBoxLayout
)

# Local Imports
from wrapper import pprint

VALID_SCAN_CODES = set(range(1, 84))
send_lock = threading.Lock()

def view_event(tcp_connection, display_num) -> None:
    """ Send the message to change to a specific view """

    msg = f"view:{display_num}\n"
    with send_lock:
        tcp_connection.sendall(msg.encode('utf-8'))
        pprint(f"Sent view: (Display - {display_num}")

def click(x,y):
    """ Get the location of the key pressed """

    win32api.SetCursorPos((x,y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,x,y,0,0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,x,y,0,0)

def key_press(key_code, param):
    """ Send key press event to the server """

    if key_code not in VALID_SCAN_CODES:
        pprint(f"{key_code} is not a valid scan code.")
        return

    tcp_connection = param
    msg = f"key:{key_code}\n"
    time.sleep(0.05)

    # Send the key press event to the server
    with send_lock:
        tcp_connection.sendall(msg.encode('utf-8'))
        pprint(f"Sent key press: {key_code}")

def fit_rect(src_w, src_h, dst_w, dst_h):
    """ Calculate the largest centered rectangle of aspect ratio that fits within the destination """

    src_ar = src_w / src_h
    dst_ar = dst_w / dst_h

    if dst_ar > src_ar:
        h = dst_h; w = int(h * src_ar)
    else:
        w = dst_w; h = int(w / src_ar)
    x = (dst_w - w) // 2; y = (dst_h - h) // 2
    return x, y, w, h

def mouse_cb(event, x, y, flag, param:list=[]):
    """ Send mouse click event to the server  """

    if event != cv2.EVENT_LBUTTONDOWN:
        return

    if not param:
        pprint("Mouse callback parameters not set.")
        return

    # Get the state and TCP connection from the parameters
    state, tcp_connection = param
    if state["frame"] is None or state["disp_rect"] is None:
        return

    disp_x, disp_y, disp_w, disp_h = state["disp_rect"]
    if not (disp_x <= x < disp_x + disp_w and disp_y <= y < disp_y + disp_h):
        pprint("Clicked outside video content")
        return

    # Get the captured screen dimensions from state
    capture_w, capture_h = state["captures"]
    rel_x = (x - disp_x) / disp_w
    rel_y = (y - disp_y) / disp_h
    screen_x = int(rel_x * capture_w)
    screen_y = int(rel_y * capture_h)

    # Send click to server instead of clicking locally
    try:
        msg = f"click:{screen_x},{screen_y}\n"
        with send_lock:
            tcp_connection.sendall(msg.encode('utf-8'))
            pprint(f"Sent click: ({screen_x},{screen_y})")
    except (BrokenPipeError, ConnectionResetError):
        pprint("Failed to send click — connection lost.")

def connect_to_server(host: str, port: int, retries: int = 5, delay: float = 1.0) -> socket.socket:
    """Establish a TCP connection to the server, with retries."""

    for attempt in range(retries):
        try:
            tcp_connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_connection.connect((host, port))
            print(f"Connected to server at {host}:{port}")
            return tcp_connection
        except (ConnectionRefusedError, OSError) as e:
            pprint(f"Connection attempt {attempt + 1}/{retries} failed: {e}")
            tcp_connection.close()
            time.sleep(delay)

    error = f"Could not connect to {host}:{port} after {retries} attempts."
    raise ConnectionRefusedError(error)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from the socket, or raise if connection closes."""

    data = b''
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
            if not packet:
                raise ConnectionError("Connection closed before all bytes were received.")
            data += packet
        except (BlockingIOError, socket.timeout):
            continue
    return data

def display_video(tcp_connection: socket.socket, display) -> None:
    """Receive video frames from the server and display them."""
    try:
        first_frame = True
        state = {"frame": None, "disp_rect": None, "captures": None}

        # Read handshake once before the loop
        # TODO: Add the number of displays avaliable ---------
        capture_w = struct.unpack('>I', recv_exact(tcp_connection, 4))[0]
        capture_h = struct.unpack('>I', recv_exact(tcp_connection, 4))[0]
        state["captures"] = [capture_w, capture_h]
        pprint(f"Server screen dimensions: {capture_w}x{capture_h}")

        # Setup window actions and callbacks
        VIDEO_WINDOW = "Nature Station"
        cv2.namedWindow(VIDEO_WINDOW, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(VIDEO_WINDOW, mouse_cb, param=[state, tcp_connection])
        kb_hook = keyboard.on_release(lambda e: key_press(e.scan_code, param=tcp_connection))

        while True:
            raw_size = recv_exact(tcp_connection, 4)
            frame_size = struct.unpack('>I', raw_size)[0]

            if frame_size == 0:
                pprint("Received zero-length frame. Skipping.")
                continue

            if frame_size > 10 * 1024 * 1024:
                pprint(f"Implausible frame size: {frame_size} bytes. Closing.")
                break

            frame_data = recv_exact(tcp_connection, frame_size)
            frame_array = np.frombuffer(frame_data, dtype=np.uint8)
            frame = cv2.imdecode(frame_array, cv2.IMREAD_COLOR)

            if frame is None:
                pprint("Failed to decode frame. Skipping.")
                continue

            state["frame"] = frame
            src_h, src_w = frame.shape[:2]
            dst_w, dst_h = src_w // 2, src_h // 2
            x, y, w, h = fit_rect(src_w, src_h, dst_w, dst_h)
            state["disp_rect"] = (x, y, w, h)

            # Scale the stream to display -----------
            canvas = np.zeros((dst_h, dst_w, 3), dtype=np.uint8)
            resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            canvas[y:y+h, x:x+w] = resized
            cv2.imshow(VIDEO_WINDOW, canvas)

            if first_frame: # Resize window to standarad
                cv2.resizeWindow(VIDEO_WINDOW, 1920, 1080)
                view_event(tcp_connection, display)
                first_frame = False

            if cv2.waitKey(1) & 0xFF == 27:
                pprint("Exiting Video Stream...")
                break

    except ConnectionError as e:
        pprint(f"Connection error: {e}")
    except struct.error as e:
        pprint(f"Failed to unpack frame size: {e}")
    finally:
        keyboard.unhook(kb_hook)
        cv2.destroyAllWindows()
        tcp_connection.close()
    pprint("Stream ended!")

# # --- Entry point ---
# host = socket.gethostname()
# ip_addr = socket.gethostbyname(host)
# port = 3000

# tcp_connection = connect_to_server(ip_addr, port)
# display_video(tcp_connection)

class ConnectWindow(QWidget):
    """ Initial Window to connect to the server and select the display to stream """

    tcp_conn = None

    def __init__(self, parent=None):
        super(ConnectWindow, self).__init__()

        if not parent:
            pprint("Parent window not provided...")
            return

        self.parent = parent

        # Store the users default info -------------
        host = socket.gethostname()
        ip_addr = socket.gethostbyname(host)
        self.socket_dict = {host: ip_addr}
        self.init_ui()
    
    def init_ui(self):
        """ Initialize the UI components of the connection window """

        hosts = list(self.socket_dict.keys())
        self.ip_combo = QComboBox()
        self.ip_combo.addItems(hosts)

        self.port_input = QLineEdit("3000")
        self.port_input.setPlaceholderText("Port")

        self.view = QLineEdit() # TODO: Change to combobox based on how many displays are avalaible 
        self.view.setPlaceholderText("Enter Display Number (0 - All, 1 - Main, etc.)")
        self.view.returnPressed.connect(self.change_view)
        # self.view.setVisible(False)

        conn_btn = QPushButton("Connect to Stream Server")
        conn_btn.clicked.connect(self.connect)

        self.info_lbl = QLabel()

        hlay = QHBoxLayout()
        hlay.addWidget(self.ip_combo)
        hlay.addWidget(self.port_input)

        vlay = QVBoxLayout()
        vlay.addLayout(hlay)
        vlay.addWidget(self.view)
        vlay.addWidget(conn_btn)
        vlay.addWidget(self.info_lbl)
        self.setLayout(vlay)

    def connect(self):
        """ Connect to the server and start the video stream """

        self.info_lbl.setText("Connecting...")

        # Attempt tp connect to server using the selected IP address
        port = self.port_input.text().strip()
        if not port.isdigit():
            self.info_lbl.setText("Invalid port number.")
            return

        # Check the ip combo ----------------
        host = self.ip_combo.currentText()
        ip_addr = self.socket_dict.get(host)
        if not ip_addr:
            pprint(f"IP address for {host} not found.")
            return

        try:
            self.tcp_conn = connect_to_server(ip_addr, int(port))
        except Exception as e:
            self.info_lbl.setText(f"Failed to connect to {host} ({ip_addr})")
            return

        # Start the video stream -----------
        self.info_lbl.setText(f"Connected to {host} ({ip_addr}). Starting stream...")
        self.view.setVisible(True)
        QApplication.processEvents()

        # Run the video thread ----------
        stream_thread = threading.Thread(
            target=display_video,
            args=(self.tcp_conn, self.view.text()),
        )
        stream_thread.start()
        stream_thread.join()
        self.tcp_conn.close()
        self.info_lbl.setText("Stream ended.")
        self.tcp_conn = None

    def change_view(self) -> None:
        """ Request to change the view """

        text = self.view.text()
        if not text:
            self.info_lbl.setText("Not a valid display...")
            return

        view_event(text)

class StreamerApp(QMainWindow):
    """ Main Viewing Display to see the selected streamer and interact with the stream settings """

    def __init__(self):
        super(StreamerApp, self).__init__()

        self.conn_win = ConnectWindow(self)
        self.init_ui()

    def init_ui(self):
        """ Initialize the UI components of the main window """

        central_widget = QWidget()



        vlay = QVBoxLayout()
        vlay.addWidget(self.conn_win)

        self.setCentralWidget(central_widget)
        self.centralWidget().setLayout(vlay)
        self.setWindowTitle("Nature Streamer")
        self.setGeometry(100, 100, 400, 200)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StreamerApp()
    win.show()
    sys.exit(app.exec())
