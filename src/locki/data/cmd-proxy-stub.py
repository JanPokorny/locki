#!/usr/bin/env python3
"""Locki command proxy stub — forwards commands to host for validated execution.

Installed as /opt/locki/bin/git and /opt/locki/bin/gh inside sandbox containers.
Connects to the host command proxy server over TCP and relays stdin/stdout/stderr.
"""

import json
import os
import socket
import struct
import sys
import threading

HOST = "host.lima.internal"
PORT = 7890

# Frame types
_H = ord("H")  # header
_I = ord("I")  # stdin
_O = ord("O")  # stdout
_E = ord("E")  # stderr
_C = ord("C")  # close stdin
_X = ord("X")  # exit code


def _send(sock, ftype, payload=b""):
    sock.sendall(bytes([ftype]) + struct.pack("!I", len(payload)) + payload)


def _recv(sock):
    buf = b""
    while len(buf) < 5:
        chunk = sock.recv(5 - len(buf))
        if not chunk:
            raise ConnectionError("Server closed connection")
        buf += chunk
    ftype = buf[0]
    length = struct.unpack("!I", buf[1:5])[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("Server closed connection")
        payload += chunk
    return ftype, payload


def main():
    argv = [os.path.basename(sys.argv[0])] + sys.argv[1:]
    cwd = os.getcwd()

    try:
        sock = socket.create_connection((HOST, PORT), timeout=10)
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        print(f"{argv[0]}: cannot connect to locki command proxy on host ({e})", file=sys.stderr)
        sys.exit(127)

    sock.settimeout(None)

    # Send header
    _send(sock, _H, json.dumps({"argv": argv, "cwd": cwd}).encode())

    # Forward stdin in a daemon thread
    def forward_stdin():
        try:
            while True:
                data = sys.stdin.buffer.read(8192)
                if not data:
                    _send(sock, _C)
                    break
                _send(sock, _I, data)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    t = threading.Thread(target=forward_stdin, daemon=True)
    t.start()

    # Receive stdout/stderr/exit from server
    exit_code = 1
    try:
        while True:
            ftype, payload = _recv(sock)
            if ftype == _O:
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()
            elif ftype == _E:
                sys.stderr.buffer.write(payload)
                sys.stderr.buffer.flush()
            elif ftype == _X:
                exit_code = struct.unpack("!i", payload)[0]
                break
    except ConnectionError:
        pass
    finally:
        sock.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
