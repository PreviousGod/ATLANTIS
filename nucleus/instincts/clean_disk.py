"""Clean disk — remove old logs, caches, temp files."""
import os
import time

def main():
    targets = ["/tmp", "/var/tmp", os.path.expanduser("~/.cache")]
    cutoff = time.time() - 7 * 86400  # 7 days old
    removed = 0
    freed = 0
    for target in targets:
        if not os.path.isdir(target):
            continue
        for root, dirs, files in os.walk(target):
            for f in files:
                path = os.path.join(root, f)
                try:
                    st = os.stat(path)
                    if st.st_mtime < cutoff and st.st_size > 0:
                        size = st.st_size
                        os.unlink(path)
                        removed += 1
                        freed += size
                except (OSError, PermissionError):
                    continue
    print(f"Cleaned: {removed} files, freed {freed // (1024*1024)}MB")

if __name__ == "__main__":
    main()
