"""Find which process is using common conflicting ports."""
import os
import socket

def main():
    common_ports = [80, 443, 3000, 5000, 8000, 8080, 8888]
    print("Port usage check:")
    for port in common_ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            result = s.connect_ex(("127.0.0.1", port))
            s.close()
            if result == 0:
                # Find PID from /proc/net/tcp
                pid = _find_pid_for_port(port)
                print(f"  :{port} — IN USE (PID: {pid or 'unknown'})")
        except Exception:
            pass

def _find_pid_for_port(port):
    hex_port = f"{port:04X}"
    try:
        with open("/proc/net/tcp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                local = parts[1]
                if local.endswith(f":{hex_port}"):
                    inode = parts[9]
                    return _inode_to_pid(inode)
    except Exception:
        pass
    return None

def _inode_to_pid(inode):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            fd_dir = f"/proc/{pid}/fd"
            for fd in os.listdir(fd_dir):
                link = os.readlink(f"{fd_dir}/{fd}")
                if f"socket:[{inode}]" in link:
                    return pid
        except (OSError, PermissionError):
            continue
    return None

if __name__ == "__main__":
    main()
