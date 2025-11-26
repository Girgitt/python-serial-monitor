#!/usr/bin/env python3
"""
serial_tee_bridge_shutdown.py — bidirectional serial <-> PTY bridge
with optional TX echo, optional auth bypass, periodic "ping",
and startup input flush to avoid mid-line desync.

ENV
  SERIAL_PORT=/dev/ttyUSB0
  BAUD=115200
  VPORTS=2
  VPORT_PREFIX=/tmp/ttyNR
  CHMOD_VPTY=666
  SHUTDOWN_SECRET=super-secret
  SHUTDOWN_VALIDATE=1      # 0 disables ts/mac validation
  ECHO_PTY_TX=1            # echo PTY TX to other PTYs (0/1)
  PING_INTERVAL_SEC=0      # >0 to send "ping\\n" every N seconds
  STARTUP_FLUSH=1          # 1 = drop any buffered input at startup
  STARTUP_FLUSH_MS=200     # drain window after flush (milliseconds)
  WARMUP_MS=200
"""

import os, sys, time, signal, select, errno, fcntl, subprocess, termios
from typing import List, Tuple, Optional
from functools import partial

try:
    import serial  # pip install pyserial
except ImportError:
    print("ERROR: pip install pyserial", file=sys.stderr)
    sys.exit(1)

EXPECTED_PREFIX = "event:shutdown"

# --- anti-spin cooldown machinery ---
FD_COOLDOWN_MS = int(os.environ.get("FD_COOLDOWN_MS", "20"))  # default 20ms
_cooldown_until = {}  # fd -> monotonic deadline


# --- force line-buffered, write-through logging to pipes (for supervisord) ---
try:
    # Python 3.7+: best option
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    # Fallback for older Pythons
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 1)  # 1 = line buffered
    sys.stderr = os.fdopen(sys.stderr.fileno(), "w", 1)

# Make all prints auto-flush just in case
print = partial(print, flush=True)


def env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def fnv1a32_keyed(secret: str, msg: str) -> str:
    h = 0x811C9DC5
    prime = 0x01000193
    data = (secret + "|" + msg).encode("utf-8")
    for b in data:
        h ^= b
        h = (h * prime) & 0xFFFFFFFF
    return f"{h:08x}"  # lowercase


def parse_line(line: str):
    if not line.startswith(EXPECTED_PREFIX):
        return None
    parts = line[len(EXPECTED_PREFIX):].strip().split()
    kv = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k] = v
    ts, mac = kv.get("ts"), kv.get("mac")
    if ts is None or mac is None:
        return None
    try:
        ts_i = int(ts, 10)
    except ValueError:
        return None
    return ts_i, mac


def poweroff():
    try:
        subprocess.run(["/usr/bin/systemctl", "poweroff"], check=False)
    except Exception as e:
        print(f"[ERR] poweroff failed: {e}", file=sys.stderr)


def set_nonblock(fd: int):
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def create_vpty_set(n: int, prefix: str, chmod_octal: int) -> List[Tuple[int, str, str]]:
    import pty
    v = []
    for i in range(1, n + 1):
        mfd, sfd = pty.openpty()
        slave_path = os.ttyname(sfd)
        try:
            os.chmod(slave_path, int(str(chmod_octal), 8))
        except Exception as e:
            print(f"[WARN] chmod({slave_path}) failed: {e}", file=sys.stderr)
        link_path = f"{prefix}{i}"
        try:
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            os.symlink(slave_path, link_path)
        except Exception as e:
            print(f"[WARN] symlink({link_path}->{slave_path}) failed: {e}", file=sys.stderr)
        os.close(sfd)          # keep only the master in this process
        set_nonblock(mfd)
        v.append((mfd, slave_path, link_path))
    return v


def startup_flush(ser, enabled: bool, drain_ms: int):
    """Drop any pre-existing bytes and brief dribble to avoid mid-line parse."""
    if not enabled:
        return
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        termios.tcflush(ser.fileno(), termios.TCIFLUSH)
    except Exception:
        pass
    deadline = time.monotonic() + max(0, drain_ms) / 1000.0
    while time.monotonic() < deadline:
        try:
            if not ser.read(65536):
                time.sleep(0.01)
        except Exception:
            break


def pop_universal_line(buf: bytearray) -> Optional[bytes]:
    """Pop next line delimited by LF or CR. Treat CRLF / LFCR as a single EOL."""
    for i, b in enumerate(buf):
        if b in (0x0A, 0x0D):  # \n or \r
            line = bytes(buf[:i])
            del buf[:i+1]
            # collapse paired delimiter if present
            if buf and ((buf[0] == 0x0A and b == 0x0D) or (buf[0] == 0x0D and b == 0x0A)):
                del buf[:1]
            return line
    return None


def _fd_ok(fd: int) -> bool:
    return time.monotonic() >= _cooldown_until.get(fd, 0.0)


def _cool(fd: int, ms: int = FD_COOLDOWN_MS):
    _cooldown_until[fd] = time.monotonic() + (ms / 1000.0)


def main():
    port = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
    baud = int(os.environ.get("BAUD", "115200"))
    vports = int(os.environ.get("VPORTS", "1"))
    prefix = os.environ.get("VPORT_PREFIX", "/tmp/ttyNR")
    chmod_vpty = int(os.environ.get("CHMOD_VPTY", "666"))
    echo_pty_tx = env_flag("ECHO_PTY_TX", False)
    echo_ping_tx = env_flag("ECHO_PING_TX", False)

    validate = env_flag("SHUTDOWN_VALIDATE", False)
    secret = os.environ.get("SHUTDOWN_SECRET", "super-secret")

    WARMUP_MS = int(os.environ.get("WARMUP_MS", "200"))
    warmup_deadline = time.monotonic() + (WARMUP_MS / 1000.0)

    # Heartbeat / ping settings
    try:
        ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "0"))
    except ValueError:
        ping_interval = 0.0
    enable_ping = ping_interval > 0.0
    next_ping = (time.monotonic() + ping_interval) if enable_ping else None

    # Startup flush settings
    flush_on_start = env_flag("STARTUP_FLUSH", True)
    try:
        flush_ms = int(os.environ.get("STARTUP_FLUSH_MS", "200"))
    except ValueError:
        flush_ms = 200

    try:
        ser = serial.Serial(port=port, baudrate=baud, timeout=0, exclusive=True)
    except Exception as e:
        print(f"[ERR] opening {port}: {e}", file=sys.stderr)
        return 2

    startup_flush(ser, flush_on_start, flush_ms)

    vpty = create_vpty_set(vports, prefix, chmod_vpty)

    print(f"[INFO] Real serial: {port} @ {baud} (exclusive).")
    for i, (_, slave, link) in enumerate(vpty, 1):
        print(f"[INFO] vTTY {i}: slave={slave}  link={link}")
    if not vpty:
        print("[WARN] No virtual ports created (VPORTS=0).")
    print(f"[INFO] ECHO_PTY_TX={'ON' if echo_pty_tx else 'OFF'}")
    print(f"[INFO] ECHO_PING_TX={'ON' if echo_ping_tx else 'OFF'}")
    print(f"[INFO] SHUTDOWN_VALIDATE={'ON' if validate else 'OFF'}")
    if enable_ping:
        print(f"[INFO] PING_INTERVAL_SEC={ping_interval:.3f}")
    else:
        print("[INFO] PING disabled")
    if flush_on_start:
        print(f"[INFO] STARTUP_FLUSH: dropped buffered input (settle {flush_ms} ms)")

    buf_serial_in = bytearray()
    last_ts_seen = -1
    running = True

    def stop(_s, _f):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    serial_fd = ser.fileno()

    try:
        while running:
            # select() timeout aligned to next ping
            timeout = 1.0
            now = time.monotonic()
            if enable_ping and next_ping is not None:
                due = max(0.0, next_ping - now)
                timeout = min(timeout, due)

            # Only include FDs not on cooldown
            read_fds = []
            if _fd_ok(serial_fd):
                read_fds.append(serial_fd)
            read_fds.extend([mfd for (mfd, _, _) in vpty if _fd_ok(mfd)])

            rlist, _, _ = select.select(read_fds, [], [], timeout)

            # Send ping if due (to REAL serial; optional echo to PTYs)
            if enable_ping and time.monotonic() >= next_ping:
                ping_msg = b"ping\n"
                try:
                    ser.write(ping_msg)
                except Exception as e:
                    print(f"[WARN] serial write (ping) failed: {e}", file=sys.stderr)

                if echo_ping_tx:
                    for (mfd, _slave, link) in vpty:
                        try:
                            os.write(mfd, ping_msg)
                        except OSError as e:
                            if e.errno not in (errno.EIO, errno.EPIPE, errno.ENXIO, errno.EBADF, errno.EAGAIN):
                                print(f"[WARN] ping write to {link} failed: {e}", file=sys.stderr)
                        except Exception as e:
                            print(f"[WARN] ping write to {link} failed: {e}", file=sys.stderr)

                next_ping = time.monotonic() + ping_interval

            moved = False  # track if any bytes were moved this iteration

            # REAL serial -> PTYs + parse shutdown
            if serial_fd in rlist:
                try:
                    if hasattr(ser, "in_waiting") and ser.in_waiting == 0:
                        _cool(serial_fd)
                        chunk = b""
                    else:
                        chunk = ser.read(4096)
                        if not chunk:
                            _cool(serial_fd)
                except Exception as e:
                    print(f"[WARN] serial read failed: {e}", file=sys.stderr)
                    _cool(serial_fd)
                    chunk = b""
                if chunk:
                    moved = True
                    # fan-out to all PTYs
                    for (mfd, _slave, link) in vpty:
                        try:
                            os.write(mfd, chunk)
                        except OSError as e:
                            if e.errno not in (errno.EIO, errno.EPIPE, errno.ENXIO, errno.EBADF, errno.EAGAIN):
                                print(f"[WARN] write to {link} failed: {e}", file=sys.stderr)
                        except Exception as e:
                            print(f"[WARN] write to {link} failed: {e}", file=sys.stderr)

                    # parse for shutdown trigger, using universal newline splitter
                    buf_serial_in.extend(chunk)
                    while True:
                        raw = pop_universal_line(buf_serial_in)
                        if raw is None:
                            break
                        try:
                            line = raw.decode("utf-8", errors="ignore").strip()
                        except Exception:
                            continue
                        if time.monotonic() < warmup_deadline:
                            continue
                        if not line:
                            continue

                        # Print any non-empty line to console (debug)
                        if len(line) > 1:
                            print(line)

                        if not line.startswith(EXPECTED_PREFIX):
                            continue

                        if not validate:
                            print("[INFO] Shutdown trigger (no validation). Powering off…")
                            poweroff()
                            running = False
                            break

                        parsed = parse_line(line)
                        if not parsed:
                            print("[WARN] Missing ts/mac while validation is ON; reject.")
                            continue

                        ts_i, mac = parsed
                        msg = f"ts={ts_i}"
                        expected = fnv1a32_keyed(secret, msg)

                        if mac != expected:
                            print(f"[WARN] MAC mismatch (got {mac}, expected {expected})")
                            continue
                        if ts_i <= last_ts_seen:
                            print(f"[WARN] Non-increasing ts ({ts_i} <= {last_ts_seen}); reject.")
                            continue

                        print("[INFO] Valid shutdown trigger received. Powering off…")
                        poweroff()
                        last_ts_seen = ts_i
                        running = False
                        break  # stop parsing; loop will exit

            # PTY -> REAL serial (+ optional echo)
            for (mfd, _slave, link) in vpty:
                if mfd not in rlist:
                    continue
                while True:
                    try:
                        data = os.read(mfd, 4096)
                    except OSError as e:
                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EIO, errno.EPIPE, errno.ENXIO, errno.EBADF):
                            _cool(mfd)
                            break
                        print(f"[WARN] read from {link} failed: {e}", file=sys.stderr)
                        _cool(mfd)
                        break
                    except Exception as e:
                        print(f"[WARN] read from {link} failed: {e}", file=sys.stderr)
                        _cool(mfd)
                        break

                    if not data:
                        _cool(mfd)
                        break

                    try:
                        ser.write(data)
                        moved = True
                    except Exception as e:
                        print(f"[WARN] serial write failed: {e}", file=sys.stderr)

                    if echo_pty_tx and data:
                        for (other_mfd, _s2, link2) in vpty:
                            if other_mfd == mfd:
                                continue
                            try:
                                os.write(other_mfd, data)
                            except OSError as e:
                                if e.errno not in (errno.EIO, errno.EPIPE, errno.ENXIO, errno.EBADF, errno.EAGAIN):
                                    print(f"[WARN] echo write to {link2} failed: {e}", file=sys.stderr)
                            except Exception as e:
                                print(f"[WARN] echo write to {link2} failed: {e}", file=sys.stderr)

            # Optional tiny throttle if nothing moved; avoids edge-case busy loops
            if not moved and not rlist:
                time.sleep(0.001)

    finally:
        try:
            ser.close()
        except Exception:
            pass
        for (mfd, _slave, _link) in vpty:
            try:
                os.close(mfd)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
