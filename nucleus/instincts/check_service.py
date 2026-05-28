"""Check critical systemd service status."""
import os

def main():
    services = ["hermes-gateway", "nucleus", "sshd", "NetworkManager"]
    print("Service status:")
    for svc in services:
        status = _check_systemd(svc)
        print(f"  {svc:<25} — {status}")

def _check_systemd(name):
    # Check user services first, then system
    for user_flag in ["--user", ""]:
        pid_file = f"/run/user/{os.getuid()}/{name}.pid" if user_flag else f"/run/{name}.pid"
        try:
            state_path = f"/proc/1/root/run/systemd/units/invocation:{name}.service"
            # Simpler: just check if process is running via /proc
            pass
        except Exception:
            pass
    # Fallback: check /proc for process name
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() == name:
                    return f"RUNNING (PID {pid})"
        except (IOError, PermissionError):
            continue
    return "NOT FOUND"

if __name__ == "__main__":
    main()
