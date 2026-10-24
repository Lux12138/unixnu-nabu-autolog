#!/bin/bash
set -e
cd ~/src/unixnu-nabu || exit 1
if [ "${NABU_NO_RESET:-0}" != "1" ]; then
  git fetch --depth 1 origin main -q && git reset --hard FETCH_HEAD -q
else
  echo "NABU_NO_RESET=1: packing from the current checkout ($(git log --oneline -1))"
fi

K=~/src/UniXNU/out/nabu/kernel/kernel.macho
A=/tmp/kernel.macho.adt

# Take the ADT parameters from board.toml instead of hardcoding them here, so the script cannot
# drift away from the configuration again.
cd ~/src/UniXNU/out/nabu/tree
python3 - "$A" <<'PY'
import subprocess
import sys
import tomllib

board = tomllib.load(open('/root/src/unixnu-nabu/board.toml', 'rb'))
adt = board['payload']['adt']

command = [
    'python3', 'scripts/make-nabu-adt.py',
    '--output', sys.argv[1],
    '--dram-size', str(adt['dram_size']),
]
for key, flag in (('owned_memory_size', '--owned-memory-size'),
                  ('root_match_volume_uuid', '--root-match-volume-uuid'),
                  ('cpu_regs', '--cpu-regs')):
    if adt.get(key) is not None:
        command += [flag, str(adt[key])]
command.append('--packed-data-ramdisk' if adt.get('packed_data_ramdisk') else '--data-ramdisk')
if adt.get('trust_cache'):
    command.append('--trust-cache')
if adt.get('ipad_identity'):
    command.append('--ipad-identity')

print(' '.join(command))
subprocess.run(command, check=True)
PY

echo ""
echo "kernel: $K ($(stat -c%s "$K") bytes)"
echo "adt:    $A ($(stat -c%s "$A") bytes)"
echo ""
cd ~/src/unixnu-nabu
bash scripts/make-cust-payload.sh "$K" "$A" /tmp/cust-final2.img 2>&1 | tail -4
cp /tmp/cust-final2.img /mnt/c/Temp/cust-final2.img
ls -lh /mnt/c/Temp/cust-final2.img
