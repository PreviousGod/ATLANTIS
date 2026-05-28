"""Senzorni Aparat — čita sistemske metrike iz /proc (Linux, 0 deps)."""
import os
import subprocess
import time


class Sensor:
    def __init__(self):
        self._prev_idle = 0
        self._prev_total = 0
        self._has_prev = False

    def read(self):
        """Vrati kompletno senzorno stanje sistema."""
        state = {
            "cpu_percent": self._read_cpu(),
            "ram_percent": self._read_ram(),
            "disk_percent": self._read_disk(),
            "disk_inode_percent": self._read_inode(),
            "load_1min": self._read_load(),
            "process_count": self._read_proc_count(),
            "zombie_count": self._read_zombies(),
            "git_dirty": self._read_git_dirty(),
            "services_down": self._read_services(),
            "swap_percent": self._read_swap(),
            "timestamp": time.time(),
        }
        return state

    def _read_cpu(self):
        try:
            with open("/proc/stat") as f:
                fields = f.readline().split()[1:]
            nums = [int(x) for x in fields]
            idle, total = nums[3], sum(nums)
            if not self._has_prev:
                self._prev_idle, self._prev_total, self._has_prev = idle, total, True
                return 0.0
            d_idle = idle - self._prev_idle
            d_total = total - self._prev_total
            self._prev_idle, self._prev_total = idle, total
            return round((1.0 - d_idle / d_total) * 100.0, 1) if d_total > 0 else 0.0
        except Exception:
            return 0.0

    def _read_ram(self):
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            total = int(lines[0].split()[1])
            avail = int(lines[2].split()[1])
            return round((total - avail) / total * 100.0, 1) if total else 0.0
        except Exception:
            return 0.0

    def _read_disk(self):
        """Root filesystem usage %."""
        try:
            st = os.statvfs("/")
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            return round(used / total * 100.0, 1) if total else 0.0
        except Exception:
            return 0.0

    def _read_inode(self):
        """Root filesystem inode usage %."""
        try:
            st = os.statvfs("/")
            total = st.f_files
            free = st.f_favail
            used = total - free
            return round(used / total * 100.0, 1) if total else 0.0
        except Exception:
            return 0.0

    def _read_load(self):
        """1-min load average."""
        try:
            with open("/proc/loadavg") as f:
                return float(f.read().split()[0])
        except Exception:
            return 0.0

    def _read_proc_count(self):
        """Broj aktivnih procesa."""
        try:
            return len([p for p in os.listdir("/proc") if p.isdigit()])
        except Exception:
            return 0

    def _read_zombies(self):
        """Broj zombie procesa."""
        try:
            count = 0
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/stat") as f:
                        state = f.read().split()[-50:]  # rough
                        if "Z" in state:
                            count += 1
                except Exception:
                    pass
            return count
        except Exception:
            return 0

    def _read_swap(self):
        """Swap usage %."""
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            total = 0
            used = 0
            for line in lines:
                if line.startswith("SwapTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("SwapFree:"):
                    free = int(line.split()[1])
                    used = total - free
            return round(used / total * 100.0, 1) if total else 0.0
        except Exception:
            return 0.0

    def _read_git_dirty(self):
        """Da li je cwd git repo sa nekomitovanim promenama."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=2, cwd=os.getcwd(),
            )
            if result.returncode != 0:
                return 0
            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            return len(lines)
        except Exception:
            return 0

    def _read_services(self):
        """Proveri da li su ključni servisi aktivni."""
        services = ["hermes-gateway", "tailscaled", "systemd-resolved"]
        down = []
        for svc in services:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=2,
                )
                if result.returncode != 0:
                    down.append(svc)
            except Exception:
                pass
        return down
