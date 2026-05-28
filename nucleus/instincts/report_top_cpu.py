"""Report top CPU consuming processes."""
import os

def main():
    lines = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read().split()
            with open(f"/proc/{pid}/comm") as f:
                name = f.read().strip()
            utime, stime = int(stat[13]), int(stat[14])
            lines.append((utime + stime, pid, name))
        except (IOError, IndexError, ValueError):
            continue
    lines.sort(reverse=True)
    print("Top CPU processes:")
    for total, pid, name in lines[:10]:
        print(f"  PID {pid:>7} | {name:<20} | ticks={total}")

if __name__ == "__main__":
    main()
