#!/usr/bin/env python3

"""
Curses-based serial monitor.

- Top window: received lines from serial.
- Bottom window (~25% height): line being typed (TX> ...).
- Press Enter to send current line (with trailing '\n').
- Press 'q' or Ctrl-C to quit.
- Up/Down arrows: navigate input history (like a simple shell).
"""

import sys
import time
import argparse
import curses

import serial  # pyserial


def get_args_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument('--port', help='serial port, e.g. /dev/ttyUSB0')
    parser.add_argument('--baud', help='baud rate, e.g. 115200')
    parser.add_argument('--timeout', help='read timeout in seconds, e.g. 0.1')

    return parser


class PythonSerialMonitor:
    def __init__(self, forced_port=None, baud=115200, timeout=0.1):
        self.forced_port = forced_port
        self.baud = baud
        self.timeout = timeout

        self.ser = None
        self.read_buffer = b''  # buffer for partial serial lines

        # For curses UI
        self.log_lines = []
        self.max_log_lines = 2000

        # History of sent commands (for Up/Down arrows)
        self.history = []           # list[str]
        self.history_pos = None     # None = not browsing, otherwise index in history

    # ---------- Serial handling ----------

    def _open_serial(self):
        # Try to open serial port. Raises on failure.
        if self.ser is not None and self.ser.is_open:
            return

        if self.forced_port:
            port = self.forced_port
            self.ser = serial.Serial(port, self.baud, timeout=self.timeout)
        else:
            baseports = ['/dev/ttyUSB', '/dev/ttyACM', 'COM', '/dev/tty.usbmodem']
            last_exc = None
            for base in baseports:
                for i in range(0, 64):
                    port = f"{base}{i}"
                    try:
                        self.ser = serial.Serial(port, self.baud, timeout=self.timeout)
                        break
                    except Exception as e:
                        self.ser = None
                        last_exc = e
                if self.ser:
                    break

            if not self.ser:
                raise serial.SerialException(
                    f"Couldn't auto-detect serial port (last error: {last_exc})"
                )

        self.ser.reset_input_buffer()
        # Compatibility attribute (pyserial ignores it, but keep from original code)
        self.ser.ReadBufferSize = 255
        self.read_buffer = b''

    def _close_serial(self):
        if self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _extract_lines(self, chunk: bytes):
        # Append new bytes to buffer and return complete decoded lines,
        # where a line is delimited by either '\n' or '\r'.
        lines = []
        if not chunk:
            return lines

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

            lines.append(decoded)

        return lines

    def _write_serial(self, text: str):
        if self.ser is None or not self.ser.is_open:
            return
        self.ser.write(text.encode())

    # ---------- Logging + UI helpers ----------

    def _add_log_line(self, text: str):
        self.log_lines.append(text)
        if len(self.log_lines) > self.max_log_lines:
            self.log_lines = self.log_lines[-self.max_log_lines :]

    def _compute_layout(self, stdscr):
        # Return (log_height, input_height, width).
        h, w = stdscr.getmaxyx()
        if h < 4:
            # Very small terminal; reserve 1 line for input.
            input_height = 1
            log_height = max(1, h - input_height)
            return log_height, input_height, w

        min_input = 3
        input_height = max(min_input, h // 4)
        if input_height > h - 1:
            input_height = h - 1
        log_height = h - input_height
        if log_height < 1:
            log_height = 1
            input_height = h - log_height
        return log_height, input_height, w

    def _render_log(self, win):
        win.erase()
        h, w = win.getmaxyx()
        if h <= 0 or w <= 0:
            return

        # Draw a box if there's room
        if h >= 3 and w >= 4:
            win.box()
            top = 1
            max_rows = h - 2
            max_cols = w - 2
        else:
            top = 0
            max_rows = h
            max_cols = w

        start = max(0, len(self.log_lines) - max_rows)
        visible = self.log_lines[start:]

        row = 0
        for line in visible:
            if row >= max_rows:
                break
            try:
                win.addnstr(top + row, 1 if h >= 3 and w >= 4 else 0, line, max_cols)
            except curses.error:
                # Ignore drawing errors on small terminals
                pass
            row += 1

        win.noutrefresh()

    def _render_input(self, win, buffer_text: str):
        win.erase()
        h, w = win.getmaxyx()
        if h <= 0 or w <= 0:
            return

        if h >= 3 and w >= 4:
            win.box()
            content_y = 1
            max_cols = w - 2
            x_offset = 1
        else:
            content_y = 0
            max_cols = w
            x_offset = 0

        prompt = "TX> "
        max_buf_len = max_cols - len(prompt)
        if max_buf_len < 1:
            # Not enough space even for prompt; just draw prompt
            try:
                win.addnstr(content_y, x_offset, prompt, max_cols)
            except curses.error:
                pass
            win.noutrefresh()
            return

        display_buffer = buffer_text[-max_buf_len:]
        line = prompt + display_buffer

        try:
            win.addnstr(content_y, x_offset, line, max_cols)
        except curses.error:
            pass

        # Move cursor to end of buffer
        cursor_x = x_offset + len(prompt) + len(display_buffer)
        if cursor_x >= w:
            cursor_x = w - 1
        try:
            win.move(content_y, cursor_x)
        except curses.error:
            pass

        win.noutrefresh()

    # ---------- History helpers ----------

    def _history_add(self, line: str):
        # Nie dodawaj pustych; nie powielaj ostatniego wpisu.
        if not line:
            return
        if self.history and self.history[-1] == line:
            return
        self.history.append(line)
        # Po wysłaniu nowej linii – wyjdź z trybu "przeglądania historii"
        self.history_pos = None

    def _history_prev(self, current_buffer: str) -> str:
        """
        Zwraca poprzedni wpis z historii (dla strzałki w górę).
        history_pos:
          - None: zaczynamy od ostatniej pozycji
          - >= 0: cofamy się o 1 (jeśli można)
        """
        if not self.history:
            return current_buffer

        if self.history_pos is None:
            # Start browsing from the most recent
            self.history_pos = len(self.history) - 1
        else:
            if self.history_pos > 0:
                self.history_pos -= 1

        return self.history[self.history_pos]

    def _history_next(self, current_buffer: str) -> str:
        """
        Zwraca następny wpis z historii (dla strzałki w dół),
        albo pusty bufor, jeśli wychodzimy poza koniec.
        """
        if not self.history or self.history_pos is None:
            # Already at "live" input
            return ""

        if self.history_pos < len(self.history) - 1:
            self.history_pos += 1
            return self.history[self.history_pos]
        else:
            # Ostatni wpis -> wyjście do pustego bufora
            self.history_pos = None
            return ""

    # ---------- Curses main loop ----------

    def _curses_main(self, stdscr):
        # Basic curses setup
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        stdscr.nodelay(True)  # non-blocking getch

        try:
            curses.curs_set(1)
        except curses.error:
            # Some terminals don't support cursor visibility changes
            pass

        log_height, input_height, width = self._compute_layout(stdscr)
        log_win = stdscr.subwin(log_height, width, 0, 0)
        input_win = stdscr.subwin(input_height, width, log_height, 0)

        # Reconnection control
        next_reconnect_time = 0.0

        input_buffer = ""

        self._add_log_line("PythonSerialMonitor (curses UI)")
        self._add_log_line("Press Ctrl-C or 'q' to quit.")
        self._add_log_line("Use Up/Down to navigate input history.")

        while True:
            now = time.time()

            # (Re-)open serial if needed
            if (self.ser is None or not self.ser.is_open) and now >= next_reconnect_time:
                try:
                    self._open_serial()
                    self._add_log_line(f"Opened serial: {self.ser.port} @ {self.baud}")
                    next_reconnect_time = now + 1.0
                except Exception as e:
                    self._add_log_line(f"ERROR opening serial: {e}")
                    next_reconnect_time = now + 2.0

            # Handle keyboard input
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            while ch != -1:
                # 'q' or Ctrl-C
                if ch in (ord('q'), ord('Q'), 3):
                    raise KeyboardInterrupt

                if ch in (10, 13):  # Enter
                    if input_buffer and self.ser is not None and self.ser.is_open:
                        try:
                            self._write_serial(input_buffer + "\n")
                            self._add_log_line(f">> {input_buffer}")
                            self._history_add(input_buffer)
                        except serial.SerialException as e:
                            self._add_log_line(f"ERROR writing to serial: {e}")
                            self._close_serial()
                            next_reconnect_time = time.time() + 2.0
                    # Zawsze czyścimy bufor po Enterze
                    input_buffer = ""
                    # Wyjście z trybu historii (na wszelki wypadek)
                    self.history_pos = None

                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    input_buffer = input_buffer[:-1]
                    # Modyfikacja ręczna -> wychodzimy z historii
                    self.history_pos = None

                elif ch == curses.KEY_UP:
                    input_buffer = self._history_prev(input_buffer)

                elif ch == curses.KEY_DOWN:
                    input_buffer = self._history_next(input_buffer)

                elif ch == curses.KEY_RESIZE:
                    # Recalculate layout & recreate windows
                    log_height, input_height, width = self._compute_layout(stdscr)
                    log_win = stdscr.subwin(log_height, width, 0, 0)
                    input_win = stdscr.subwin(input_height, width, log_height, 0)

                else:
                    # Regular printable characters
                    if 0 <= ch <= 255:
                        c = chr(ch)
                        if c.isprintable():
                            input_buffer += c
                            # Edycja ręczna = wyjście z trybu historii
                            self.history_pos = None

                try:
                    ch = stdscr.getch()
                except curses.error:
                    ch = -1

            # Handle serial input
            if self.ser is not None and self.ser.is_open:
                try:
                    waiting = self.ser.in_waiting
                except serial.SerialException as e:
                    self._add_log_line(f"Serial exception (in_waiting): {e}")
                    self._close_serial()
                    next_reconnect_time = time.time() + 2.0
                    waiting = 0

                if waiting:
                    try:
                        chunk = self.ser.read(waiting)
                    except serial.SerialException as e:
                        self._add_log_line(f"Serial exception (read): {e}")
                        self._close_serial()
                        next_reconnect_time = time.time() + 2.0
                    else:
                        for line in self._extract_lines(chunk):
                            self._add_log_line(line)

            # Draw UI
            self._render_log(log_win)
            self._render_input(input_win, input_buffer)
            curses.doupdate()

            time.sleep(0.01)

    def run(self):
        try:
            curses.wrapper(self._curses_main)
        finally:
            self._close_serial()


def main():
    args = get_args_parser().parse_args()

    port = args.port if args.port else None
    baud = int(args.baud) if args.baud else 115200
    timeout = float(args.timeout) if args.timeout else 0.1

    psm = PythonSerialMonitor(forced_port=port, baud=baud, timeout=timeout)

    try:
        psm.run()
    except KeyboardInterrupt:
        # curses.wrapper już przywrócił terminal
        print("Monitor: Keyboard Interrupt. Exiting Now...")
    except Exception as e:
        print(f"Unhandled exception: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
