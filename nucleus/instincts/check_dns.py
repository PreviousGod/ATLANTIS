"""Check DNS resolution status."""
import socket
import time

def main():
    targets = ["google.com", "cloudflare.com", "github.com"]
    print("DNS resolution check:")
    for host in targets:
        start = time.time()
        try:
            ip = socket.gethostbyname(host)
            ms = (time.time() - start) * 1000
            print(f"  {host:<20} → {ip:<16} ({ms:.0f}ms)")
        except socket.gaierror as e:
            print(f"  {host:<20} → FAILED: {e}")

if __name__ == "__main__":
    main()
