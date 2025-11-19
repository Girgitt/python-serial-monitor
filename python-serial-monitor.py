#!/usr/bin/env python3

# Some of this monitor was made possible with help from those at:
# http://shallowsky.com/blog/hardware/ardmonitor.html
# http://code.activestate.com/recipes/134892/
# Girgitt: modified for python3 and improved for Linux
#
# Updated:
# - Fix broken terminal after exit (restore termios)
# - Add line buffering so serial output is not split/mangled
# - Pass baud/timeout from CLI

import sys
import threading
import time
import queue as Queue
import serial
import argparse
import atexit


def get_args_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument('--port', help='serial port, e.g. /dev/ttyUSB0')
    parser.add_argument('--baud', help='baud rate, e.g. 115200')
    parser.add_argument('--timeout', help='read timeout in seconds, e.g. 0.1')

    return parser


class PythonSerialMonitor():
    def __init__(self, forced_port=None, baud=115200, timeout=0.1):
        self.windows = False
        self.unix = False
        self.fd = None
        self.old_settings = None
        self.forced_port = forced_port
        self.baud = baud
        self.timeout = timeout

        self.ser = None
        self.read_buffer = b''  # buffer for partial serial lines

        # Detect platform and set up terminal
        try:
            # Windows
            import msvcrt  # noqa: F401
            self.windows = True
        except ImportError:
            # Unix-like
            import tty
            import termios

            if sys.stdin.isatty():
                self.fd = sys.stdin.fileno()
                # Save original settings
                self.old_settings = termios.tcgetattr(self.fd)
                # Put terminal in cbreak mode (char-by-char, no line buffering)
                tty.setcbreak(self.fd)
                self.unix = True

                # Ensure cleanup happens on normal interpreter exit
                atexit.register(self.cleanUp)
            else:
                # Non-tty stdin (pipe/file); do not touch termios
                self.unix = False

        self.input_queue = Queue.Queue()
        self.stop_queue = Queue.Queue()
        self.pause_queue = Queue.Queue()

        self.input_thread = threading.Thread(
            target=self.add_input,
            args=(self.input_queue, self.stop_queue, self.pause_queue,)
        )
        self.input_thread.daemon = True
        self.input_thread.start()

    def getch(self):
        """Read one character from stdin (used in background thread)."""
        if self.unix:
            try:
                ch = sys.stdin.read(1)
            except Exception as e:
                print(f"unhandled exception on stdin.read(1): {e}", file=sys.stderr)
                ch = ''
            return ch
        if self.windows:
            import msvcrt
            try:
                return msvcrt.getch().decode(errors='ignore')
            except Exception:
                return ''
        # Fallback
        try:
            ch = sys.stdin.read(1)
        except Exception:
            ch = ''
        return ch

    def cleanUp(self):
        """Restore original terminal settings (idempotent)."""
        if self.unix and self.fd is not None and self.old_settings is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
            except termios.error:
                # Terminal may already be closed or not a tty
                pass

    def add_input(self, input_queue, stop_queue, pause_queue):
        """Background thread: read keystrokes and push to input_queue."""
        while True:
            ch = self.getch()
            if ch:
                input_queue.put(ch)

            # Handle pause/resume (kept for compatibility)
            if not pause_queue.empty():
                msg = pause_queue.get()
                if msg == 'pause':
                    # Wait until 'resume'
                    while True:
                        if not pause_queue.empty():
                            if pause_queue.get() == 'resume':
                                break

            # Handle stop
            if not stop_queue.empty():
                msg = stop_queue.get()
                if msg == 'stop':
                    break

    def _open_serial(self):
        baseports = []
        if self.forced_port is None:
            baseports = ['/dev/ttyUSB', '/dev/ttyACM', 'COM', '/dev/tty.usbmodem1234']

        if baseports:
            # Autodetect port
            while not self.ser:
                for baseport in baseports:
                    if self.ser:
                        break
                    for i in range(0, 64):
                        try:
                            port = baseport + str(i)
                            self.ser = serial.Serial(port, self.baud, timeout=self.timeout)
                            print("Monitor: Opened " + port)
                            return
                        except Exception:
                            self.ser = None
                            pass

                if not self.ser:
                    print("Monitor: Couldn't open a serial port.")
                    print("Monitor: Press 'enter' to try again or 'esc' to exit.")
                    while True:
                        if not self.input_queue.empty():
                            keyboardInput = self.input_queue.get()
                            if not keyboardInput:
                                continue
                            if ord(keyboardInput) == 27:  # ESC
                                self.stop_queue.put('stop')
                                self.cleanUp()
                                sys.exit(1)
                            elif keyboardInput in ('\n', '\r'):
                                # retry scanning ports
                                break
                            else:
                                # Any other key also retries
                                break
        elif self.forced_port is not None:
            print(f"trying port: {self.forced_port}")
            self.ser = serial.Serial(self.forced_port, self.baud, timeout=self.timeout)
            print("Monitor: Opened " + self.forced_port)
        else:
            print("port not provided and default search misconfigured")
            self.stop_queue.put('stop')
            self.cleanUp()
            sys.exit(1)

    def _process_serial_chunk(self, chunk: bytes):
        """
        Append new bytes to buffer and emit complete lines, where a line is
        delimited by either '\\n' or '\\r'.
        """
        if not chunk:
            return

        self.read_buffer += chunk

        while True:
            idx_r = self.read_buffer.find(b'\r')
            idx_n = self.read_buffer.find(b'\n')
            idxs = [i for i in (idx_r, idx_n) if i != -1]

            if not idxs:
                # no full line yet
                break

            idx = min(idxs)
            line_bytes = self.read_buffer[:idx]
            # drop the delimiter
            self.read_buffer = self.read_buffer[idx + 1 :]

            if not line_bytes:
                # empty line (e.g. from \r\n sequence)
                continue

            try:
                decoded = line_bytes.decode(errors='replace')
            except Exception:
                decoded = repr(line_bytes)

            print("received: " + decoded, flush=True)

    def run(self):
        # Ensure serial is open
        if self.ser is None or not self.ser.is_open:
            self._open_serial()

        # reset buffer for this session
        self.read_buffer = b''

        # Serial configuration
        self.ser.flushInput()
        # Compatibility attribute (not used by pyserial)
        self.ser.ReadBufferSize = 255

        # Main I/O loop
        while True:
            # Keyboard -> serial
            if not self.input_queue.empty():
                keyboardInput = self.input_queue.get()
                if keyboardInput:
                    print("sent: " + repr(keyboardInput), flush=True)
                    try:
                        self.ser.write(keyboardInput.encode())
                    except serial.SerialException:
                        # propagate to outer loop
                        raise

            # Serial -> stdout with line buffering
            try:
                bytes_to_read = self.ser.in_waiting  # or inWaiting()
                if bytes_to_read:
                    chunk = self.ser.read(bytes_to_read)
                    self._process_serial_chunk(chunk)
            except serial.SerialException:
                # propagate to outer loop
                raise
            except IOError:
                # propagate to outer loop
                raise


if __name__ == '__main__':
    args = get_args_parser().parse_args()

    port = args.port if args.port else None
    baud = int(args.baud) if args.baud else 115200
    timeout = float(args.timeout) if args.timeout else 0.1

    psm = PythonSerialMonitor(forced_port=port, baud=baud, timeout=timeout)

    while True:
        try:
            psm.run()
        except serial.SerialException:
            print("Monitor: Disconnected (Serial exception)")
            # try to reopen in next loop iteration
            psm.ser = None
            time.sleep(1)
        except IOError:
            print("Monitor: Disconnected (I/O Error)")
            psm.ser = None
            time.sleep(1)
        except KeyboardInterrupt:
            print("Monitor: Keyboard Interrupt. Exiting Now...")
            psm.stop_queue.put('stop')
            psm.cleanUp()
            sys.exit(0)
        except Exception as e:
            print(f"unhandled exception: {e}", file=sys.stderr)
            psm.cleanUp()
            sys.exit(1)
