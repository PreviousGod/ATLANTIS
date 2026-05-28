"""Find and report zombie processes."""
import os
import signal

def main():
    zombies = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("State:") and "Z" in line:
                        with open(f"/proc/{pid}/comm") as c:
                            name = c.read().strip()
                        zombies.append((pid, name))
                    break
        except (IOError, PermissionError):
            continue
    if not zombies:
        print("No zombie processes found.")
        return
    print(f"Found {len(zombies)} zombie(s):")
    for pid, name in zombies:
        print(f"  PID {pid} — {name}")
        # Try to reap by signaling parent
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        os.kill(ppid, signal.SIGCHLD)
                        break
        except (IOError, OSError, ValueError):
            pass

if __name__ == "__main__":
    main()
