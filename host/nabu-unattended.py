#!/usr/bin/env python3
"""Unattended round trip: boot the kernel, wait for its panic reset, read the log back.

The kernel mirrors the console into a ring, and on panic writes the ring to the
diagnostic window at the tail of userdata (native_ufs_log_*, 32 MiB) before a PSCI
SYSTEM_RESET. This script drives that cycle from the host:

    fastboot boot <kernel>  ->  wait for the board to leave and come back
    adb (Android/TWRP) or a TWRP boot  ->  dd the window out of userdata
    decode the NABULOG1 header and print the interesting lines

Only the cust partition is ever written, and only when --cust is given. Nothing
here touches abl/xbl/bootloader.
"""
import argparse
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ADB = r'C:\platform-tools\adb.exe'
FASTBOOT = r'C:\platform-tools\fastboot.exe'

# GPT facts read back from this board: the partition table uses 4096-byte LBAs
# (logical_block_size reports 4096). userdata runs from USERDATA_FIRST to
# USERDATA_LAST and the diagnostic window is its last LOG_BLOCKS blocks. These
# values are the live table -- an older set of constants was 4.5 GiB too high at
# the end, which read past the partition and stalled the UFS.
USERDATA_FIRST = 0x288040
USERDATA_LAST = 0x769E7FA
SECTOR = 4096
LOG_BLOCKS = 8192

MAGIC = b'NABULOG1'
HEADER_BYTES = 4096


def run(command, timeout=60):
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print('  timed out: %s' % ' '.join(command), file=sys.stderr)
        return None


def fastboot_devices():
    result = run([FASTBOOT, 'devices'], timeout=20)
    if result is None:
        return []
    # During re-enumeration fastboot prints an unusable row (the serial reads as
    # "????????????"); accepting it made a still-booting board look like it had reset.
    text = result.stdout.decode('ascii', 'replace')
    serials = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == 'fastboot' and re.fullmatch(r'[0-9A-Za-z]{6,20}', parts[0]):
            serials.append(parts[0])
    return serials


def adb_devices():
    result = run([ADB, 'devices'], timeout=20)
    if result is None:
        return []
    text = result.stdout.decode('utf-8', 'replace')
    serials = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ('device', 'recovery'):
            serials.append(parts[0])
    return serials


def pick_serial(requested):
    fastboot = fastboot_devices()
    adb = adb_devices()
    if requested:
        return requested
    for serial in fastboot + adb:
        return serial
    raise SystemExit('no board on adb or fastboot')


def wait_leave(serial, timeout):
    start = time.time()
    while time.time() - start < timeout:
        if serial not in fastboot_devices():
            return True
        time.sleep(10)
    return False


def wait_return(serial, timeout):
    """Wait for the board to appear again after the reset. Returns ('fastboot'|'adb', serial)."""
    start = time.time()
    while time.time() - start < timeout:
        adb = adb_devices()
        if adb:
            return 'adb', (serial if serial in adb else adb[0])
        fastboot = fastboot_devices()
        if serial in fastboot:
            return 'fastboot', serial
        time.sleep(10)
    return None, None


def pull_window(serial, skip_blocks):
    # exec-out of 32 MiB over adb has stalled this TWRP twice; dump on the device and
    # pull the file instead. The window is small and tmpfs holds it easily.
    remote = '/tmp/nabuwin.bin'
    command = ('dd if=/dev/block/by-name/userdata bs=%d skip=%d count=%d of=%s 2>/dev/null; ls -l %s'
               % (SECTOR, skip_blocks, LOG_BLOCKS, remote, remote))
    result = run([ADB, '-s', serial, 'shell', command], timeout=180)
    if result is None or not result.stdout.strip():
        return None
    local = Path(tempfile.gettempdir()) / 'nabuwin.bin'
    if local.exists():
        local.unlink()
    result = run([ADB, '-s', serial, 'pull', remote, str(local)], timeout=180)
    if result is None or not local.is_file():
        return None
    return local.read_bytes()


def decode(raw):
    if len(raw) < HEADER_BYTES:
        raise SystemExit('window is %d bytes, shorter than one header block' % len(raw))
    if raw[:8] != MAGIC:
        raise SystemExit('no NABULOG1 header; first 8 bytes are %r' % raw[:8])
    version, flags, length, dropped = struct.unpack_from('<IIII', raw, 8)
    total, = struct.unpack_from('<Q', raw, 24)
    if version != 1:
        raise SystemExit('unsupported NABULOG1 version %d' % version)
    if length > len(raw) - HEADER_BYTES:
        raise SystemExit('header claims %d bytes but the window holds %d'
                         % (length, len(raw) - HEADER_BYTES))
    return flags, length, dropped, total, raw[HEADER_BYTES:HEADER_BYTES + length]


INTERESTING = re.compile(
    r'panic|SIGKILL|OS_REASON|Executable|launchd|nabu:|IOFindBSDRoot|ufs:|'
    r'Trust|amfi|AMFI|Task|proc', re.IGNORECASE)


def summarize(text, limit):
    lines = text.decode('utf-8', 'replace').splitlines()
    hits = [line for line in lines if INTERESTING.search(line)]
    selected = hits[-limit:] if hits else lines[-limit:]
    for line in selected:
        print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--kernel', help='kernel image to boot')
    parser.add_argument('--cust', help='payload to flash to cust first (never anything else)')
    parser.add_argument('--twrp', help='TWRP image to boot when the reset leaves the board in fastboot')
    parser.add_argument('--serial', help='board serial; default is the only device present')
    parser.add_argument('--timeout', type=int, default=300, help='seconds to wait for the reset')
    parser.add_argument('--raw-out', default='nabu-log-window.bin', help='where to keep the raw window')
    parser.add_argument('--text-out', default='nabu-log.txt', help='where to keep the decoded text')
    parser.add_argument('--log-offset', type=int,
                        default=(USERDATA_LAST + 1 - LOG_BLOCKS) - USERDATA_FIRST,
                        help='window offset in 4 KiB blocks from the start of userdata')
    parser.add_argument('--no-boot', action='store_true', help='only read, do not boot anything')
    parser.add_argument('--wait-fastboot', type=int, default=0,
                        help='wait this many seconds for the board to appear on fastboot first')
    args = parser.parse_args()

    if args.wait_fastboot:
        print('== waiting up to %d s for fastboot' % args.wait_fastboot)
        start = time.time()
        while time.time() - start < args.wait_fastboot:
            if fastboot_devices():
                break
            time.sleep(10)
        else:
            raise SystemExit('board never appeared on fastboot')

    serial = pick_serial(args.serial)

    if not args.no_boot:
        if not args.kernel:
            raise SystemExit('--kernel is required unless --no-boot')
        if args.cust:
            print('== flash cust %s' % args.cust)
            result = run([FASTBOOT, '-s', serial, 'flash', 'cust', args.cust], timeout=300)
            if result is None or result.returncode != 0:
                raise SystemExit('cust flash failed: %s'
                                 % (result.stderr.decode('utf-8', 'replace') if result else 'timeout'))
        print('== boot %s' % args.kernel)
        result = run([FASTBOOT, '-s', serial, 'boot', args.kernel], timeout=180)
        if result is None or result.returncode != 0:
            raise SystemExit('fastboot boot failed: %s'
                             % (result.stderr.decode('utf-8', 'replace') if result else 'timeout'))
        if not wait_leave(serial, 30):
            raise SystemExit('board never left fastboot; the kernel image did not start')
        print('== booted, waiting up to %d s for the panic reset' % args.timeout)
        kind, serial = wait_return(serial, args.timeout)
        if kind is None:
            raise SystemExit('board did not come back; did the panic path reach PSCI reset?')
        print('== board is back on %s as %s' % (kind, serial))

        if kind == 'fastboot':
            if not args.twrp:
                raise SystemExit('reset landed in fastboot and no --twrp was given. '
                                 'fastboot cannot read partitions here, so boot TWRP and rerun '
                                 'with --no-boot (or pass --twrp).')
            print('== boot TWRP %s' % args.twrp)
            result = run([FASTBOOT, '-s', serial, 'boot', args.twrp], timeout=180)
            if result is None or result.returncode != 0:
                raise SystemExit('TWRP boot failed')
            kind, serial = wait_return(serial, 180)
            if kind != 'adb':
                raise SystemExit('TWRP did not come up on adb (got %r)' % kind)

    print('== read the diagnostic window (offset %d blocks)' % args.log_offset)
    raw = pull_window(serial, args.log_offset)
    if raw is None:
        raise SystemExit('could not read the window: adb dd returned neither the header nor a full window')

    Path(args.raw_out).write_bytes(raw)
    flags, length, dropped, total, text = decode(raw)
    Path(args.text_out).write_bytes(text)
    print('== NABULOG1 v1 panic=%d bytes=%d dropped=%d total=%d'
          % (int(bool(flags & 1)), length, dropped, total))
    print('== raw %s, text %s' % (args.raw_out, args.text_out))
    print('== last interesting lines:')
    summarize(text, 60)


if __name__ == '__main__':
    main()
