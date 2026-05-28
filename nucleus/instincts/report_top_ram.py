"""Report top RAM consuming processes."""
import os

def main():
    lines = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                status = f.read()
            name = ""
            rss = 0
            for line in status.splitlines():
                if line.startswith("Name:"):
                    name = line.split(":")[1].strip()
                elif line.startswith("VmRSS:"):
                    rss = int(line.split()[1])  # kB
            if rss > 0:
                lines.append((rss, pid, name))
        except (IOError, ValueError):
            continue
    lines.sort(reverse=True)
    print("Top RAM processes:")
    for rss, pid, name in lines[:10]:
        print(f"  PID {pid:>7} | {name:<20} | RSS={rss // 1024}MB")

if __name__ == "__main__":
    main()
