import os
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
from wrapper import pprint, read_yaml

VALID_SCAN_CODES = set(range(1, 84))
send_lock = threading.Lock()

def view_event(tcp_connection, display_num) -> None:
    """ Send the message to change to a specific view """

    msg = f"view:{display_num}\n"
    with send_lock:
        tcp_connection.sendall(msg.encode('utf-8'))
        pprint(f"Sent view: (View {display_num})")

def click(x,y):
    """ Get the location of the key pressed """

    win32api.SetCursorPos((x,y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,x,y,0,0)
    time.sleep(0.005)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,x,y,0,0)

def key_press(key_code, param):
    """ Send key press event to the server """

    tcp_connection = param
    msg = f"key:{key_code}\n"
    time.sleep(0.005)

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

class ConnectWindow(QWidget):
    """ Initial Window to connect to the server and select the display to stream """

    tcp_conn = None
    running = False

    def __init__(self, parent=None):
        super(ConnectWindow, self).__init__()

        if not parent:
            pprint("Parent window not provided...")
            return

        self.parent = parent
        curr_dir = os.getcwd()
        addr_pth = os.path.join(curr_dir, "address.yaml")

        # Get the addresses --------
        self.socket_dict = read_yaml(addr_pth)
        self.init_ui()
    
    def init_ui(self):
        """ Initialize the UI components of the connection window """

        hosts = list(self.socket_dict.keys())
        self.ip_combo = QComboBox()
        self.ip_combo.addItems(hosts)
        self.ip_combo.setFixedWidth(250)

        self.port_input = QLineEdit("3000")
        self.port_input.setPlaceholderText("Port")
        self.port_input.setFixedWidth(100)

        self.view_combo = QComboBox() # TODO: Change to combobox based on how many displays are avalaible 
        self.view_combo.currentIndexChanged.connect(self.change_view)
        self.view_combo.setFixedWidth(250)

        self.conn_btn = QPushButton("Connect to Stream Server")
        self.conn_btn.clicked.connect(self.connect)

        self.info_lbl = QLabel("Not Connected.")
        self.stream_lbl = QLabel("Click Connect To View")
        self.stream_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stream_lbl.setScaledContents(True)
        # self.stream_lbl.installEventFilter()

        hlay = QHBoxLayout()
        hlay.addWidget(self.ip_combo)
        hlay.addWidget(self.port_input)
        hlay.addWidget(self.view_combo)
        hlay.addStretch()

        vlay = QVBoxLayout()
        vlay.addLayout(hlay)
        vlay.addWidget(self.info_lbl)
        vlay.addStretch()

        mlay = QVBoxLayout()
        mlay.addLayout(vlay)
        mlay.addWidget(self.stream_lbl)
        mlay.addWidget(self.conn_btn)
        self.setLayout(mlay)

    def disconnect(self):
        """ Stop the stream """

        self.view_combo.clear()
        self.info_lbl.setText("Stream ended.")
        self.stream_lbl.setText("Click Connect To View")
        self.conn_btn.setText("Connect")

        # Check if connect is open
        if self.tcp_conn:
            self.tcp_conn.close()

    def connect(self):
        """ Connect to the server and start the video stream """

        if self.running:
            """ Close the connection """

            pprint("Closing connection!")
            self.disconnect()
            self.running = False
            return
    
        # Attempt tp connect to server using the selected IP address
        self.info_lbl.setText("Connecting...")
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
        self.conn_btn.setText("Disconnect")
            
        # Toggle streaming status -----
        self.running = not self.running
        QApplication.processEvents()

        # # Run the video thread ----------
        thread = threading.Thread(target=self.display_video)
        thread.start()

    def change_view(self, index) -> None:
        """ Request to change the view """

        view_event(self.tcp_conn, display_num=index)

    def display_video(self) -> None:
        """Receive video frames from the server and display them."""

        try:
            display = 0
            first_frame = True
            state = {"frame": None, "disp_rect": None, "captures": None}

            # Read handshake once before the loop
            # TODO: Add the number of displays avaliable ---------
            capture_w = struct.unpack('>I', recv_exact(self.tcp_conn, 4))[0]
            capture_h = struct.unpack('>I', recv_exact(self.tcp_conn, 4))[0]
            num_displays = struct.unpack('>I', recv_exact(self.tcp_conn, 4))[0]


            # Store values about display --------------------
            state["captures"] = [capture_w, capture_h]
            views = [f"View {i}" for i in range(num_displays)]

            # Set main display as default
            if len(views) >= 1:
                display = 1

            self.view_combo.addItems(views)
            self.view_combo.setCurrentIndex(display)
            self.stream_lbl.setMaximumSize(capture_w, capture_h)

            # Enable key functions ---------------
            kb_hook = keyboard.on_release(lambda e: key_press(e.name, param=self.tcp_conn))
            pprint(f"Server screen dimensions: ({capture_w}x{capture_h}) Total Displays: {num_displays}")

            while True:
                raw_size = recv_exact(self.tcp_conn, 4)
                frame_size = struct.unpack('>I', raw_size)[0]

                if frame_size == 0:
                    pprint("Received zero-length frame. Skipping.")
                    continue

                if frame_size > 10 * 1024 * 1024:
                    pprint(f"Implausible frame size: {frame_size} bytes. Closing.")
                    break

                frame_data = recv_exact(self.tcp_conn, frame_size)
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
                # canvas = np.zeros((dst_h, dst_w, 3), dtype=np.uint8)
                # resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                # canvas[y:y+h, x:x+w] = resized
        
                # Convert frame to RGB format
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                h, w, ch = image.shape
                bytes_per_line = ch * w
                qt_image = QImage(image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                pixmap = QPixmap.fromImage(qt_image)
                self.stream_lbl.setPixmap(pixmap)

                if first_frame: 
                    # View Main Display to start
                    view_event(self.tcp_conn, display)
                    first_frame = False

        except ConnectionError as e:
            self.info_lbl.setText(f"Connection error: {e}")
        except struct.error as e:
            self.info_lbl.setText(f"Failed to unpack frame size: {e}")
        finally:
            keyboard.unhook(kb_hook)
            self.disconnect()

class StreamerApp(QMainWindow):
    """ Main Viewing Display to see the selected streamer and interact with the stream settings """

    def __init__(self):
        super(StreamerApp, self).__init__()

        self.manager = ConnectWindow(self)
        self.init_ui()

    def init_ui(self):
        """ Initialize the UI components of the main window """

        central_widget = QWidget()
        vlay = QVBoxLayout()
        vlay.addWidget(self.manager)

        self.setCentralWidget(central_widget)
        self.centralWidget().setLayout(vlay)
        self.setWindowTitle("Nature Streamer")
        self.resize(1280, 720)

    def closeEvent(self, a0):
        """ Action to do before closing """

        self.manager.disconnect()
        return super().closeEvent(a0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StreamerApp()
    win.show()
    sys.exit(app.exec())
