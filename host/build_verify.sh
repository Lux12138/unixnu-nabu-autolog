#!/bin/bash
# Build, then PROVE the kernel was regenerated and carries the new probes.
set -o pipefail
cd ~/src/UniXNU || exit 1

export XNU_LINUX_PREFIX=$HOME/.local/xnu-linux
source ~/src/xnu-linux-toolchain/env.sh
TREE=$PWD/out/nabu/tree
FAKE=$TREE/.cache/fakeroot
export UNIXNU_SDKROOT=$FAKE
export UNIXNU_SDK_VERSION=17.0
export UNIXNU_SDK_BUILD=21A326
export HOST_OS_VERSION=14.0
export UNIXNU_HOST_CCTOOLS_INCLUDE=$XNU_LINUX_PREFIX/cctools/include
export HOST_COMPAT_CFLAGS="-include $FAKE/usr/include/compat/hostcompat.h -I$FAKE/usr/include/compat -idirafter $UNIXNU_HOST_CCTOOLS_INCLUDE -idirafter $TREE/.cache/xnu-work/libkern -idirafter $UNIXNU_HOST_CCTOOLS_INCLUDE/foreign"
export HOST_COMPAT_LIBS="-L$UNIXNU_HOST_CCTOOLS_INCLUDE/../lib -L$FAKE/usr/lib -lmacho -lstuff -lhostcompat -luuid"
export CCC_OVERRIDE_OPTIONS="+-Wno-unknown-warning-option +-Wno-error=missing-field-initializers +-Wno-error=unused-but-set-variable +-Wno-macro-redefined +-Wno-builtin-macro-redefined"
# Must stay 0: with 1, build-arm-driver.py compiles NativeUfs as a read-only register probe
# that publishes no block device at all ("No MMIO store, no DMA, no SCSI command, no block
# device"). Without a block device there is no IOMedia, so IOFindBSDRoot can never find a
# root volume. It is useful for a first register survey, not for booting.
# 0 = real block driver. The binder supplies IOBlockStorageDevice at fixed addresses,
# so this no longer needs the read-only probe workaround.
export XNU_NABU_UFS_REGISTER_PROBE=0
# XNU_NATIVE_UFS and the other CONFIG_XNU_* switches come from configs/nabu_defconfig; unixnu injects
# them itself. Exporting them here duplicated that and fought the script's bootstrap pass, which sets
# them back to 0 on a record-less build.

# boot_progress stays off. It is the project's clean log channel -- it writes through
# boot_progress_putc rather than kprintf, so it never fights the console -- but the build refuses to
# enable it unless XNU_NATIVE_VIRTIO_BLOCK=1, XNU_NATIVE_UFS=1, XNU_OWNER_ROOT_AUDIT=1 and
# XNU_SOURCE_CORECRYPTO14=1 all hold, because its transport is virtio block. There is no virtio
# backend on this hardware, so that channel is not available here.
#
# On real hardware the way to keep the screen readable is nabu_mark_num(label, value, row): it draws
# to a fixed row and overwrites it each call, so it cannot scroll, stack, or collide with the
# console or with the marker squares.
export XNU_BOOT_PROGRESS=0

if [ "${NABU_NO_RESET:-0}" != "1" ]; then
  git fetch --depth 1 origin main 2>&1 | tail -1
  git reset --hard FETCH_HEAD 2>&1 | tail -1
  git -C ~/src/unixnu-nabu fetch --depth 1 origin main 2>&1 | tail -1
  git -C ~/src/unixnu-nabu reset --hard FETCH_HEAD 2>&1 | tail -1
else
  echo "NABU_NO_RESET=1: building the current checkout"
  git log --oneline -1
fi

if [ ! -f out/nabu/tree/.cache/xnu-work/iokit/DriverKit/IORPC.h ]; then
  echo "=== restoring DriverKit headers ==="
  ( cd out/nabu/tree && python3 scripts/prepare-xnu-source.py 2>&1 | tail -2 )
fi

mkdir -p out/nabu/tree/refer
[ -d out/nabu/tree/refer/IOStorageFamily/.git ] || \
  git clone -q --filter=blob:none https://github.com/apple-oss-distributions/IOStorageFamily.git \
    out/nabu/tree/refer/IOStorageFamily
git -C out/nabu/tree/refer/IOStorageFamily checkout -q e45fbe30a7879e0b16e26dddb78538f1b00e0bff 2>/dev/null || true

echo ""
echo "=== touching cached sources ==="
# xnu names its C++ objects .cpo, not .o. Touching the source (rather than deleting the object)
# still forces a rebuild, but keeps every object in link.filelist so the archive builders can
# read the kernel's symbols: arch 10 imports _panic, which only debug.o defines, and a deleted
# debug.o made every build abort with "Unavailable real kernel imports".
touch out/nabu/tree/.cache/xnu-work/bsd/kern/bsd_init.c \
      out/nabu/tree/.cache/xnu-work/iokit/Kernel/IOKitBSDInit.cpp \
      out/nabu/tree/.cache/xnu-work/iokit/Kernel/IOService.cpp \
      out/nabu/tree/.cache/xnu-work/osfmk/kern/debug.c \
      out/nabu/tree/.cache/xnu-work/osfmk/console/serial_general.c 2>/dev/null
echo "  done"



BEFORE=$(stat -c %Y out/nabu/kernel/kernel.macho 2>/dev/null || echo 0)

echo ""
echo "# archive 10 needs link.filelist, which the link step produces. Without it every import looks missing."
python3 /mnt/c/Users/Administrator/Documents/UniXNU/scripts/patch-archive10-wsl.py > /dev/null 2>&1 || true

echo ""
echo "=== build ==="
# NativeUfs::read() referenced timerTicks/native_ufs_read_ticks, which only exist under
# NATIVE_VERIFIED_BLOCK. In --unverified mode the file therefore did not compile, and the bring-up
# had been hiding behind XNU_NABU_UFS_REGISTER_PROBE=1, which skips read() and publishes no block
# device at all. Guard the timing the same way verify() already is, before the archive step.
if [ -f "$HOME/src/UniXNU/drivers/NativeUfs/NativeUfs.cpp" ]; then
  python3 /mnt/c/Temp/auto/patch_read_wsl.py > /dev/null 2>&1 || true
fi

# The first invocation of build-xnu-4k-baseline.sh with no compile records is the
# bootstrap pass, which forces XNU_NATIVE_UFS=0 and therefore skips linking
# build/storage-abi/IOBlockStorageDevice-reference.o. Run it once to produce the
# records (its exit status is expected to be non-zero), then the real build follows.
if [ ! -f out/nabu/tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/osfmk/DEVELOPMENT/startup.o.json ]; then
  echo "=== bootstrap pass (compile records only) ==="
  # The inserter rewrites IOKitBSDInit.cpp every build; make only recompiles when the object is older,
# so force this one. Same trap STATUS.md records for bsd_init.c.
find "$HOME/src/UniXNU/out/nabu/tree/build" -name 'IOKitBSDInit.o*' -delete 2>/dev/null || true

./unixnu build kernel -D ~/src/unixnu-nabu > /tmp/build-bootstrap.log 2>&1 || true
  echo "  startup.o.json: $(ls out/nabu/tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/osfmk/DEVELOPMENT/startup.o.json 2>/dev/null || echo missing)"
fi

# The bindings generator reads build/native-prelink/*-candidate.macho plus its .json plan. The build
# never produces those, but the previously prelinked kernel is exactly equivalent: its plan carries
# candidate_sha256, the pinned original_sha256, and the IOStorageFamily extents.
if [ -f out/nabu/kernel/kernel.macho ] && [ -f out/nabu/kernel/kernel.json ]; then
  mkdir -p out/nabu/tree/build/native-prelink
  cp out/nabu/kernel/kernel.macho out/nabu/tree/build/native-prelink/native-owner-mobile-graphics-candidate.macho
  cp out/nabu/kernel/kernel.json  out/nabu/tree/build/native-prelink/native-owner-mobile-graphics-candidate.json
  echo "  staged prelink candidate from the previous kernel"
fi

./unixnu build kernel -D ~/src/unixnu-nabu > /tmp/build-full.log 2>&1
RC=$?

AFTER=$(stat -c %Y out/nabu/kernel/kernel.macho 2>/dev/null || echo 0)

echo "  exit=$RC"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "  KERNEL NOT REGENERATED -- build did not reach the link step"
  echo ""
  echo "  first errors:"
  grep -a 'error:' /tmp/build-full.log 2>/dev/null | grep -av warning | head -6
else
  echo "  kernel regenerated ($(stat -c %y out/nabu/kernel/kernel.macho 2>/dev/null))"
  echo ""
  echo "  status-row labels (want 1 each):"
  for p in 'wfs sec' 'wfs returned' 'IOFindBSDRoot' 'walking IOMedia'; do
    n=$(strings out/nabu/kernel/kernel.macho 2>/dev/null | grep -c "$p")
    echo "    $n  $p"
  done
  echo ""
  echo "  unattended strings (want 1 each):"
  for p in 'NABULOG1' 'nabu:reset' 'nabu:log-written' 'nabu-panic-hold'; do
    n=$(strings out/nabu/kernel/kernel.macho 2>/dev/null | grep -c "$p")
    echo "    $n  $p"
  done
  echo ""
  echo "  boot-progress mode: $(cat out/nabu/tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/.boot-progress-mode 2>/dev/null)"
  echo ""
  echo "  kprintf noise (want 0 each):"
  for p in 'NABU: ROOT wfms enter' 'NABU: wfs enter' 'NABU: wfms enter timeout'; do
    n=$(strings out/nabu/kernel/kernel.macho 2>/dev/null | grep -c "$p")
    echo "    $n  $p"
  done
fi
