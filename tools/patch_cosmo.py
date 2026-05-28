#!/usr/bin/env python3
"""Cosmo APK one-shot patcher — AiFeature 108 → 105 (Nano V3 → V2).

Usage: python3 patch_cosmo.py [apk_path]
Default APK: COSMO_v1.1_REPAIRED_INSTALLABLE_API35_SIGNED.apk in ~/Desktop

Does everything in one shot: extract → patch bytecode → patch string → 
rebuild → sign → verify. No LLM needed.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

APK_DEFAULT = Path.home() / "Desktop" / "COSMO_v1.1_REPAIRED_INSTALLABLE_API35_SIGNED.apk"
KEYSTORE = Path.home() / ".hermes" / "cosmo_keys.jks"
KEYSTORE_PASS = "cosmo123"
KEY_ALIAS = "cosmo"

# ── Step 1: Find APK ───────────────────────────────────────────────────
apk_path = Path(sys.argv[1]) if len(sys.argv) > 1 else APK_DEFAULT
if not apk_path.exists():
    print(f"ERROR: APK not found at {apk_path}")
    sys.exit(1)
print(f"[1/7] APK: {apk_path} ({apk_path.stat().st_size:,} bytes)")

# ── Step 2: Extract classes38.dex ──────────────────────────────────────
work = Path(tempfile.mkdtemp(prefix="cosmo_patch_"))
print(f"[2/7] Extracting classes38.dex to {work}")
subprocess.run(["unzip", "-o", str(apk_path), "classes38.dex", "-d", str(work)],
               capture_output=True, check=True)
dex_path = work / "classes38.dex"
data = bytearray(dex_path.read_bytes())

# ── Step 3: Patch AiFeature ID 108 → 105 ───────────────────────────────
print("[3/7] Patching AiFeature ID 108 → 105")
patched_count = 0
for i in range(len(data) - 3):
    if data[i] == 0x13:  # const/16 opcode
        val = struct.unpack_from('<H', data, i + 2)[0]
        if val == 108:
            # Change 108 (0x6C) → 105 (0x69)
            struct.pack_into('<H', data, i + 2, 105)
            patched_count += 1
            print(f"  Patched const/16 v{data[i+1]}, #108→#105 at offset 0x{i:x}")
print(f"  Total: {patched_count} bytecode patches")

# ── Step 4: Patch error string ─────────────────────────────────────────
print("[4/7] Patching error message string")
old = b"AiFeature 108 (LLM-Nano V3) not found."
new = b"AiFeature 105 (LLM-Nano V2) not found."
idx = data.find(old)
if idx >= 0:
    data[idx:idx+len(old)] = new
    print(f"  String patched at offset 0x{idx:x}")
else:
    print("  String not found (may already be patched)")

dex_path.write_bytes(data)
print(f"  classes38.dex written ({len(data):,} bytes)")

# ── Step 5: Repackage APK ──────────────────────────────────────────────
print("[5/7] Repackaging APK")
rebuilt = work / "rebuilt.apk"
with tempfile.TemporaryDirectory() as tmp:
    # Extract full APK
    extract_dir = Path(tmp) / "extracted"
    extract_dir.mkdir()
    subprocess.run(["unzip", "-o", str(apk_path), "-d", str(extract_dir)],
                   capture_output=True, check=True)
    # Replace classes38.dex
    shutil.copy2(work / "classes38.dex", extract_dir / "classes38.dex")
    # Delete old signing files
    for meta in extract_dir.glob("META-INF/*"):
        if meta.name not in ("MANIFEST.MF",):
            meta.unlink()
    # Repack
    subprocess.run(
        ["zip", "-r", "-0", str(rebuilt)] + [str(p.relative_to(tmp)) for p in extract_dir.rglob("*") if p.is_file()],
        cwd=tmp, capture_output=True, check=True,
    )
subprocess.run(["zipalign", "-p", "4", str(rebuilt), str(work / "aligned.apk")],
               capture_output=True, check=True)
print(f"  Rebuilt: {rebuilt.stat().st_size:,} bytes")

# ── Step 6: Sign ────────────────────────────────────────────────────────
print("[6/7] Signing APK")
signed = work / "COSMO_PATCHED.apk"
if not KEYSTORE.exists():
    print("  Generating keystore...")
    subprocess.run([
        "keytool", "-genkey", "-v", "-keystore", str(KEYSTORE),
        "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
        "-validity", "10000", "-storepass", KEYSTORE_PASS,
        "-keypass", KEYSTORE_PASS,
        "-dname", "CN=CosmoPatch"
    ], capture_output=True, check=True)

subprocess.run([
    "apksigner", "sign", "--ks", str(KEYSTORE),
    "--ks-pass", f"pass:{KEYSTORE_PASS}",
    "--key-pass", f"pass:{KEYSTORE_PASS}",
    "--out", str(signed), str(work / "aligned.apk")
], capture_output=True, check=True)

# ── Step 7: Verify ──────────────────────────────────────────────────────
print("[7/7] Verifying")
# Check signature
result = subprocess.run(["apksigner", "verify", "--print-certs", str(signed)],
                        capture_output=True, text=True)
if result.returncode == 0:
    print("  ✓ Signature verified")
else:
    print(f"  ✗ Signature check: {result.stderr}")

# Check the patch stuck
check_data = bytearray(dex_path.read_bytes())
check_count = sum(1 for i in range(len(check_data)-3)
                  if check_data[i] == 0x13
                  and struct.unpack_from('<H', check_data, i+2)[0] == 105)
old_count = sum(1 for i in range(len(check_data)-3)
                if check_data[i] == 0x13
                and struct.unpack_from('<H', check_data, i+2)[0] == 108)
print(f"  ✓ AiFeature 105 instances: {check_count}")
print(f"  ✓ AiFeature 108 remaining: {old_count}")

print(f"\n✓ DONE: {signed}")
print(f"  Install: adb install -r {signed}")
