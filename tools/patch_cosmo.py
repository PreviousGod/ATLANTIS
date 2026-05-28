#!/usr/bin/env python3
"""Cosmo APK one-shot patcher — AiFeature 108 → 105 (Nano V3 → V2).

Pure Python. No zip, zipalign, or apksigner needed.
Handles .so native libraries correctly (stored uncompressed).

Usage: python3 patch_cosmo.py [apk_path]
"""
import os
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

APK_DEFAULT = Path.home() / "Desktop" / "COSMO_v1.1_REPAIRED_INSTALLABLE_API35_SIGNED.apk"


def patch_cosmo(apk_path: Path):
    work = Path(tempfile.mkdtemp(prefix="cosmo_patch_"))
    print(f"[1/5] APK: {apk_path} ({apk_path.stat().st_size:,} bytes)")

    # ── Step 1: Extract classes38.dex ─────────────────────────────────
    print("[2/5] Extracting and patching classes38.dex")
    dex_data = None
    with zipfile.ZipFile(apk_path, 'r') as zf:
        dex_data = bytearray(zf.read("classes38.dex"))

    # Patch bytecode: const/16 vX, #108 → const/16 vX, #105
    patched = 0
    for i in range(len(dex_data) - 3):
        if dex_data[i] == 0x13:
            val = struct.unpack_from('<H', dex_data, i + 2)[0]
            if val == 108:
                struct.pack_into('<H', dex_data, i + 2, 105)
                patched += 1
    print(f"  Bytecode patches: {patched} (108→105)")

    # Patch error string
    old = b"AiFeature 108 (LLM-Nano V3) not found."
    new = b"AiFeature 105 (LLM-Nano V2) not found."
    idx = dex_data.find(old)
    if idx >= 0:
        dex_data[idx:idx + len(old)] = new
        print(f"  String patched at offset 0x{idx:x}")
    else:
        print("  String already patched or not found")

    # ── Step 2: Repackage APK preserving .so files uncompressed ──────
    print("[3/5] Repackaging APK (preserving native libs)")
    rebuilt = work / "cosmo_rebuilt.apk"

    NATIVE_EXTENSIONS = {'.so'}
    with zipfile.ZipFile(apk_path, 'r') as zf_in:
        with zipfile.ZipFile(rebuilt, 'w', zipfile.ZIP_DEFLATED) as zf_out:
            for item in zf_in.infolist():
                # Skip old signing files
                if item.filename.startswith('META-INF/') and not item.filename.endswith('MANIFEST.MF'):
                    continue

                data = zf_in.read(item.filename)

                # Replace classes38.dex with patched version
                if item.filename == 'classes38.dex':
                    data = bytes(dex_data)

                # Native libs must be STORED (uncompressed) for Android to load them
                ext = Path(item.filename).suffix.lower()
                if ext in NATIVE_EXTENSIONS:
                    zf_out.writestr(item, data, compress_type=zipfile.ZIP_STORED)
                else:
                    zf_out.writestr(item, data, compress_type=zipfile.ZIP_DEFLATED)

    rebuilt_size = rebuilt.stat().st_size
    print(f"  Rebuilt: {rebuilt_size:,} bytes")

    # ── Step 3: Align (Python implementation) ─────────────────────────
    print("[4/5] Aligning APK")
    aligned = work / "cosmo_aligned.apk"
    _zipalign_py(rebuilt, aligned)
    print(f"  Aligned: {aligned.stat().st_size:,} bytes")

    # ── Step 4: Sign with uber-apk-signer or jarsigner ─────────────────
    print("[5/5] Signing APK")
    signed = _sign_apk(aligned, work)
    if signed:
        print(f"\n✓ DONE: {signed}")
        print(f"  adb install -r {signed}")
        # Copy to Desktop
        dest = Path.home() / "Desktop" / f"COSMO_PATCHED_NANO_V2.apk"
        shutil.copy2(signed, dest)
        print(f"  Copied to: {dest}")
    else:
        print("\n✗ Signing failed. Falling back to unsigned APK.")
        print(f"  Unsigned: {aligned}")
        print(f"  You must sign manually before installing.")


def _zipalign_py(src: Path, dst: Path, alignment: int = 4):
    """Python implementation of zipalign — aligns uncompressed entries to 4-byte boundaries."""
    ALIGNMENT = alignment

    with zipfile.ZipFile(src, 'r') as zf_in:
        with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zf_out:
            for item in zf_in.infolist():
                data = zf_in.read(item.filename)

                if item.compress_type == zipfile.ZIP_STORED:
                    # Calculate extra padding needed
                    # Get current position in output by checking header + data size
                    header_size = (
                        30  # local file header
                        + len(item.filename.encode('utf-8'))
                        + len(item.extra)
                    )
                    current_pos = header_size
                    padding_needed = (ALIGNMENT - (current_pos % ALIGNMENT)) % ALIGNMENT

                    if padding_needed > 0:
                        # Add extra field for alignment
                        extra = item.extra + b'\x00' * padding_needed
                        # Update extra field length in a copy
                        new_item = zipfile.ZipInfo(item.filename)
                        new_item.compress_type = item.compress_type
                        new_item.extra = extra
                        zf_out.writestr(new_item, data)
                    else:
                        zf_out.writestr(item, data)
                else:
                    zf_out.writestr(item, data)


def _sign_apk(apk_path: Path, work_dir: Path) -> Path | None:
    """Try to sign the APK. Tries multiple methods."""
    signed = work_dir / "COSMO_PATCHED.apk"

    # Method 1: uber-apk-signer (if installed)
    uber = shutil.which("uber-apk-signer")
    if uber:
        import subprocess
        r = subprocess.run(
            [uber, "--apks", str(apk_path), "--out", str(work_dir)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            # Find the signed output
            for f in work_dir.rglob("*.apk"):
                if "aligned" not in f.name and f != apk_path:
                    shutil.move(str(f), str(signed))
                    return signed
            return apk_path  # fallback

    # Method 2: apksigner
    apksigner = shutil.which("apksigner")
    if apksigner:
        import subprocess
        ks = Path.home() / ".hermes" / "cosmo_keys.jks"
        if not ks.exists():
            subprocess.run([
                "keytool", "-genkey", "-v", "-keystore", str(ks),
                "-alias", "cosmo", "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "10000", "-storepass", "cosmo123", "-keypass", "cosmo123",
                "-dname", "CN=CosmoPatch"
            ], capture_output=True)
        r = subprocess.run([
            apksigner, "sign", "--ks", str(ks),
            "--ks-pass", "pass:cosmo123", "--key-pass", "pass:cosmo123",
            "--out", str(signed), str(apk_path),
        ], capture_output=True, text=True)
        if r.returncode == 0:
            return signed
        print(f"  apksigner failed: {r.stderr}")

    # Method 3: jarsigner (v1 only, may not work on API 35+)
    jarsigner = shutil.which("jarsigner")
    if jarsigner:
        import subprocess
        ks = Path.home() / ".hermes" / "cosmo_keys.jks"
        if not ks.exists():
            subprocess.run([
                "keytool", "-genkey", "-v", "-keystore", str(ks),
                "-alias", "cosmo", "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "10000", "-storepass", "cosmo123", "-keypass", "cosmo123",
                "-dname", "CN=CosmoPatch"
            ], capture_output=True)
        r = subprocess.run([
            jarsigner, "-keystore", str(ks), "-storepass", "cosmo123",
            "-signedjar", str(signed), str(apk_path), "cosmo",
        ], capture_output=True, text=True)
        if r.returncode == 0:
            print("  ⚠ Signed with jarsigner (v1 only — may not work on API 35+)")
            return signed

    return None


if __name__ == "__main__":
    apk = Path(sys.argv[1]) if len(sys.argv) > 1 else APK_DEFAULT
    if not apk.exists():
        print(f"ERROR: {apk} not found")
        sys.exit(1)
    patch_cosmo(apk)
