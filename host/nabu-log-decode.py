#!/usr/bin/env python3
"""Decode the NABULOG1 block that the kernel writes at the tail of userdata.

The kernel's panic path copies its console ring to the diagnostic log window
(native_ufs_log_*, the last 32 MiB of userdata) as:

    block 0  header, little endian
             0   8  "NABULOG1"
             8   4  version (1)
            12   4  flags (bit 0 = panic flush)
            16   4  bytes of ring data that follow the header
            20   4  bytes dropped after the ring wrapped
            24   8  total bytes ever captured
    block 1+ ring contents in time order, zero padded to 4096

So the payload starts at byte 4096 of the window, not at the end of the header.
"""
import argparse
import json
import struct
import sys

MAGIC = b'NABULOG1'
HEADER_BYTES = 4096


def decode(raw):
    if len(raw) < HEADER_BYTES:
        raise SystemExit('window is %d bytes, shorter than one 4 KiB header block' % len(raw))
    if raw[:8] != MAGIC:
        raise SystemExit('no NABULOG1 header; first 8 bytes are %r' % raw[:8])
    version, flags, length, dropped = struct.unpack_from('<IIII', raw, 8)
    total, = struct.unpack_from('<Q', raw, 24)
    if version != 1:
        raise SystemExit('unsupported NABULOG1 version %d' % version)
    if length > len(raw) - HEADER_BYTES:
        raise SystemExit('header claims %d bytes but the window holds %d'
                         % (length, len(raw) - HEADER_BYTES))
    return {
        'version': version,
        'panic': bool(flags & 1),
        'flags': flags,
        'length': length,
        'dropped': dropped,
        'total': total,
        'text': raw[HEADER_BYTES:HEADER_BYTES + length],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('window', help='raw 32 MiB window pulled from userdata')
    parser.add_argument('--out', help='write the decoded text here instead of stdout')
    parser.add_argument('--json', action='store_true', help='print the header as JSON on stderr')
    args = parser.parse_args()

    raw = open(args.window, 'rb').read()
    log = decode(raw)
    text = log.pop('text')
    if args.json:
        print(json.dumps(log), file=sys.stderr)
    else:
        print('NABULOG1 v%d panic=%s bytes=%d dropped=%d total=%d'
              % (log['version'], int(log['panic']), log['length'], log['dropped'], log['total']),
              file=sys.stderr)

    if args.out:
        open(args.out, 'wb').write(text)
    else:
        sys.stdout.buffer.write(text)


if __name__ == '__main__':
    main()
