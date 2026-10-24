#!/usr/bin/env python3
"""Capture console output in a ring, write it to the UFS log window on panic, then reset.

Two files are touched:

  osfmk/console/serial_general.c
      every character that passes through console_printbuf_putc is mirrored into a
      4 MiB ring; nabu_log_flush() writes a header and the ring to the diagnostic
      log window (the last 32 MiB of userdata, native_ufs_log_*), syncs it and
      paints a marker. nabu_psci_reset() issues PSCI SYSTEM_RESET (SMC 0x84000009).

  osfmk/kern/debug.c
      nabu_panic_restart() calls the flush and the reset from kdp_machine_reboot_type()
      and from panic_spin_forever(), which are the two ends of the panic path.

PEHaltRestart() is deliberately not used: PE_halt_restart is NULL on this port, so
PEHaltRestart(kPEPanicRestartCPU) returns -1 and halts in place. The working reset
primitive is the one ArmDTPlatform::platformStageReset() already uses.
"""
import io
import sys

REQUESTED = ('console_printbuf_putc',)


def read(path):
    return io.open(path, encoding='utf-8').read()


def write(path, text):
    io.open(path, 'w', encoding='utf-8', newline='').write(text)


LOG_VERSION = 6

LOG_BLOCK = r'''
/* nabu-log-version: 6 */
/* ---- nabu: panic log capture -------------------------------------------------
 * Every character that reaches the console is mirrored into a ring. On panic the
 * ring and a header are written to the diagnostic log window at the end of the
 * userdata partition (native_ufs_log_*), synced, and the board is reset through
 * PSCI SYSTEM_RESET. The log survives the reset; the host reads it back from the
 * same window after the board comes up again.
 *
 * Header block layout (little endian):
 *   0  8  "NABULOG1"
 *   8  4  version (1)
 *  12  4  flags (1 = panic flush)
 *  16  4  bytes that follow the header
 *  20  4  bytes dropped after the ring wrapped
 *  24  8  total bytes ever captured
 */
#define NABU_LOG_RING_BYTES  (4u * 1024u * 1024u)
#define NABU_LOG_RING_MASK   (NABU_LOG_RING_BYTES - 1u)
#define NABU_LOG_CHUNK_BYTES 65536u

static char nabu_log_ring[NABU_LOG_RING_BYTES];
static char nabu_log_bounce[NABU_LOG_CHUNK_BYTES];
static char nabu_log_block[4096];
static unsigned int nabu_log_head;
static unsigned long long nabu_log_total;
static unsigned int nabu_log_dropped;
static int nabu_log_opened;
static int nabu_log_flushed;
static int nabu_log_hold;

extern void nabu_mark_text(const char *s, unsigned int y);
extern int native_ufs_log_open(uint64_t *first_lba, uint64_t *blocks, const char **why);
extern int native_ufs_log_write(uint64_t rel, uint32_t blocks, const void *src);
extern int native_ufs_log_sync(void);

void
nabu_log_putc(int ch)
{
	nabu_log_ring[nabu_log_head] = (char)ch;
	nabu_log_head = (nabu_log_head + 1u) & NABU_LOG_RING_MASK;
	if (nabu_log_total < NABU_LOG_RING_BYTES) {
		nabu_log_total++;
	} else {
		nabu_log_dropped++;
	}
}

static void
nabu_log_write32(char *at, unsigned int value)
{
	at[0] = (char)(value & 0xffu);
	at[1] = (char)((value >> 8) & 0xffu);
	at[2] = (char)((value >> 16) & 0xffu);
	at[3] = (char)((value >> 24) & 0xffu);
}

static void
nabu_log_write64(char *at, unsigned long long value)
{
	nabu_log_write32(at, (unsigned int)(value & 0xffffffffu));
	nabu_log_write32(at + 4, (unsigned int)(value >> 32));
}

static int
nabu_log_fail(const char *why)
{
	nabu_mark_text(why, 2340);
	/* Push whatever already landed (at minimum the header block) to the medium,
	 * so the host can tell an open failure from a data-write failure. */
	native_ufs_log_sync();
	return 0;
}

/* Panic-time transport: DRAM above the kernel's maxmem, not UFS. The flush path
 * takes the UFS host lock, and on hardware 2026-10-24 that never returned once the
 * other CPUs were halted -- the board sat there instead of resetting. This window
 * is mapped once at boot (nabu_panic_save_init) and the kernel never allocates it,
 * so the copy is a plain memcpy. The next boot reads it back and writes it out. */
static char *nabu_save_dram;
static unsigned long long nabu_save_bytes;
static int nabu_log_restored;

void
nabu_panic_save_init(void *dram, unsigned long long bytes)
{
	nabu_save_dram = (char *)dram;
	nabu_save_bytes = bytes;
}

static unsigned int
nabu_log_read32(const char *at)
{
	return (unsigned int)(unsigned char)at[0] |
	    ((unsigned int)(unsigned char)at[1] << 8) |
	    ((unsigned int)(unsigned char)at[2] << 16) |
	    ((unsigned int)(unsigned char)at[3] << 24);
}

static int
nabu_save_has_space(void)
{
	return nabu_save_dram != 0 && nabu_save_bytes >= 4096ull + NABU_LOG_RING_BYTES;
}

static int
nabu_save_has_magic(void)
{
	return nabu_save_has_space() &&
	    nabu_save_dram[0] == 'N' && nabu_save_dram[1] == 'A' && nabu_save_dram[2] == 'B' &&
	    nabu_save_dram[3] == 'U' && nabu_save_dram[4] == 'S' && nabu_save_dram[5] == 'A' &&
	    nabu_save_dram[6] == 'V' && nabu_save_dram[7] == '1';
}

void
nabu_panic_save(void)
{
	unsigned int i;

	if (!nabu_save_has_space()) {
		return;
	}
	nabu_save_dram[0] = 'N'; nabu_save_dram[1] = 'A'; nabu_save_dram[2] = 'B'; nabu_save_dram[3] = 'U';
	nabu_save_dram[4] = 'S'; nabu_save_dram[5] = 'A'; nabu_save_dram[6] = 'V'; nabu_save_dram[7] = '1';
	nabu_log_write64(nabu_save_dram + 8, nabu_log_total);
	nabu_log_write32(nabu_save_dram + 16, nabu_log_head);
	nabu_log_write32(nabu_save_dram + 20, nabu_log_dropped);
	for (i = 0; i < NABU_LOG_RING_BYTES; ++i) {
		nabu_save_dram[4096 + i] = nabu_log_ring[i];
	}
}

void
nabu_panic_restore(void)
{
	unsigned int i;

	if (!nabu_save_has_magic()) {
		return;
	}
	nabu_log_total = (unsigned long long)nabu_log_read32(nabu_save_dram + 8) |
	    ((unsigned long long)nabu_log_read32(nabu_save_dram + 12) << 32);
	nabu_log_head = nabu_log_read32(nabu_save_dram + 16);
	nabu_log_dropped = nabu_log_read32(nabu_save_dram + 20);
	for (i = 0; i < NABU_LOG_RING_BYTES; ++i) {
		nabu_log_ring[i] = nabu_save_dram[4096 + i];
	}
	nabu_log_restored = 1;
	nabu_save_dram[0] = 0;
}

int
nabu_log_flush(int is_panic)
{
	uint64_t first = 0, window = 0;
	uint64_t rel;
	const char *why = "";
	unsigned long long available;
	unsigned int offset, length, blocks, i;

	if (!nabu_log_opened) {
		if (!native_ufs_log_open(&first, &window, &why)) {
			/* why names the refusal ("off", "no root layout", "window refused", ...). */
			nabu_log_fail(why);
			nabu_mark_text("nabu:log-nowin", 2300);
			return 0;
		}
		nabu_log_opened = 1;
	}

	available = nabu_log_total;
	for (i = 0; i < sizeof(nabu_log_block); ++i) {
		nabu_log_block[i] = 0;
	}
	nabu_log_block[0] = 'N';
	nabu_log_block[1] = 'A';
	nabu_log_block[2] = 'B';
	nabu_log_block[3] = 'U';
	nabu_log_block[4] = 'L';
	nabu_log_block[5] = 'O';
	nabu_log_block[6] = 'G';
	nabu_log_block[7] = '1';
	nabu_log_write32(nabu_log_block + 8, 1u);
	nabu_log_write32(nabu_log_block + 12, is_panic ? 1u : 0u);
	nabu_log_write32(nabu_log_block + 16, (unsigned int)available);
	nabu_log_write32(nabu_log_block + 20, nabu_log_dropped);
	nabu_log_write64(nabu_log_block + 24, nabu_log_total);
	if (!native_ufs_log_write(0, 1, nabu_log_block)) {
		return nabu_log_fail("nabu:log-fail");
	}
	/* Land the header before the bulk copy: if the data path fails or the reset
	 * arrives early, the window still carries NABULOG1 and the open succeeded. */
	(void)native_ufs_log_sync();

	offset = (unsigned int)((nabu_log_head + NABU_LOG_RING_BYTES - available) & NABU_LOG_RING_MASK);
	rel = 1;
	while (available) {
		length = available > NABU_LOG_CHUNK_BYTES ? NABU_LOG_CHUNK_BYTES : (unsigned int)available;
		for (i = 0; i < length; ++i) {
			nabu_log_bounce[i] = nabu_log_ring[(offset + i) & NABU_LOG_RING_MASK];
		}
		blocks = (length + 4095u) / 4096u;
		for (i = length; i < blocks * 4096u; ++i) {
			nabu_log_bounce[i] = 0;
		}
		if (!native_ufs_log_write(rel, blocks, nabu_log_bounce)) {
			return nabu_log_fail("nabu:log-fail");
		}
		rel += blocks;
		offset = (offset + length) & NABU_LOG_RING_MASK;
		available -= length;
	}
	(void)native_ufs_log_sync();
	nabu_log_flushed = 1;
	nabu_mark_text("nabu:log-written", 2340);
	return 1;
}

/* Called by the UFS driver once the root windows are published. Opening and writing
 * here, in ordinary process context, proves the window and the write path and puts
 * the boot half of the ring on the medium even if the panic path cannot write. */
void
nabu_log_boot_probe(void)
{
	nabu_mark_text("nabu:probe", 2200);
	/* If the last reset came from panic(), its tail is still in the reserved DRAM
	 * window. Put it in the ring first so this boot persists that, not its own. */
	nabu_panic_restore();
	(void)nabu_log_flush(nabu_log_restored ? 1 : 0);
	if (nabu_log_restored) {
		nabu_log_hold = 1;
		nabu_log_restored = 0;
	}
}

/* Rewrite the window from a healthy kernel thread. panic() itself must not touch
 * UFS: on hardware 2026-10-24 the flush there took the host lock and never returned,
 * so the board never reset. The periodic copy is at most one interval stale.
 * hold is set when the window was just filled with the previous panic's tail; the
 * next boot must not overwrite it from this boot's (uninteresting) ring. */
void
nabu_log_periodic(void)
{
	if (nabu_log_hold || !nabu_log_opened) {
		return;
	}
	(void)nabu_log_flush(0);
}

void
nabu_psci_reset(void)
{
	register uint64_t x0 __asm__("x0") = 0x84000009ULL;
	register uint64_t x1 __asm__("x1") = 0;
	register uint64_t x2 __asm__("x2") = 0;
	register uint64_t x3 __asm__("x3") = 0;
	__asm__ volatile ("smc #0" : "+r" (x0) : "r" (x1), "r" (x2), "r" (x3) : "memory");
	for (;;) {
		__asm__ volatile ("wfe");
	}
}
/* ---- end nabu panic log ---------------------------------------------------- */
'''

DEBUG_HELPER = r'''
/* nabu: panic restart ---------------------------------------------------------
 * Reset through PSCI SYSTEM_RESET. The reset is direct because PE_halt_restart
 * is NULL on this port: PEHaltRestart() only returns -1 and halts in place.
 * Boot arg nabu-panic-restart selects how much runs before the reset:
 *   0  off
 *   1  reset only (default; proves the SMC path without touching UFS)
 *   2  flush the console ring to the UFS log window, then reset
 * Every step paints a marker, so a screen that stops moving says which one hung.
 */
extern int nabu_log_flush(int is_panic);
extern void nabu_panic_save(void);
extern void nabu_psci_reset(void);
extern void nabu_mark_text(const char *s, unsigned int y);

static void
nabu_panic_restart(void)
{
	/* nabu:saved -- panic only copies the ring to the reserved DRAM window and
	 * resets. It must not touch UFS: the flush there held the host lock and the
	 * board never reset on hardware 2026-10-24. The next boot persists the copy. */
	int mode = 2;
	PE_parse_boot_argn("nabu-panic-restart", &mode, sizeof(mode));
	if (mode == 0) {
		nabu_mark_text("nabu:panic-off", 2500);
		return;
	}
	nabu_mark_text("nabu:panic", 2500);
	if (mode >= 2) {
		nabu_mark_text("nabu:save", 2500);
		nabu_panic_save();
		nabu_mark_text("nabu:saved", 2500);
	}
	nabu_mark_text("nabu:reset", 2420);
	nabu_psci_reset();
	nabu_mark_text("nabu:reset-failed", 2500);
}
/* ---- end nabu panic restart ------------------------------------------------ */
'''


def patch_console(path):
    src = read(path)
    report = []
    marker = '/* nabu-log-version: %d */' % LOG_VERSION
    if marker in src:
        return ['  skip   serial_general.c: log block v%d present' % LOG_VERSION]
    if 'nabu: panic log capture' in src:
        # A tree patched with an older block. The hook point is unchanged; replace
        # the whole block so new helpers and fixes actually reach the kernel.
        start = src.find('/* ---- nabu: panic log capture')
        end = src.find('/* ---- end nabu panic log', start)
        if start < 0 or end < 0:
            return ['  MISS   serial_general.c: unterminated log block']
        end = src.find('\n', end) + 1
        write(path, src[:start] + LOG_BLOCK.strip() + '\n' + src[end:])
        return ['  update serial_general.c: log block to v%d' % LOG_VERSION]
    anchor = 'void\nconsole_printbuf_putc(int ch, void * arg)\n{\n'
    if anchor not in src:
        return ['  MISS   serial_general.c: console_printbuf_putc anchor']
    replacement = ('/* nabu: panic log capture */\n'
                   'void nabu_log_putc(int ch);\n\n'
                   'void\nconsole_printbuf_putc(int ch, void * arg)\n{\n'
                   '\tnabu_log_putc(ch);\n')
    src = src.replace(anchor, replacement, 1)
    src = src.rstrip() + '\n' + LOG_BLOCK
    write(path, src)
    report.append('  insert serial_general.c: ring + flush + PSCI reset')
    return report


def patch_debug(path):
    src = read(path)
    report = []
    if 'nabu:saved' in src:
        return ['  skip   debug.c: hook present']
    if '/* nabu: panic restart' in src:
        # Upgrade a hook written before the flush became optional: the call sites are
        # unchanged, only the helper body moves.
        start = src.find('/* nabu: panic restart')
        end = src.find('/* ---- end nabu panic restart', start)
        if end < 0:
            return ['  MISS   debug.c: unterminated helper block']
        end = src.find('\n', end) + 1
        write(path, src[:start] + DEBUG_HELPER.strip() + '\n' + src[end:])
        return ['  replace debug.c: helper upgraded to the staged reset']

    helper_anchor = 'void\npanic_spin_forever(void)\n{\n'
    if helper_anchor not in src:
        return ['  MISS   debug.c: panic_spin_forever anchor']
    src = src.replace(helper_anchor,
                      DEBUG_HELPER + '\nvoid\npanic_spin_forever(void)\n{\n\tnabu_panic_restart();\n',
                      1)
    report.append('  insert debug.c: helper + panic_spin_forever hook')

    reboot_anchor = ('static void\n'
                     'kdp_machine_reboot_type(unsigned int type, uint64_t debugger_flags)\n'
                     '{\n')
    if reboot_anchor not in src:
        return report + ['  MISS   debug.c: kdp_machine_reboot_type anchor']
    src = src.replace(reboot_anchor,
                      reboot_anchor + '\tif (type == kPEPanicRestartCPU) {\n'
                                      '\t\tnabu_panic_restart();\n'
                                      '\t}\n',
                      1)
    report.append('  insert debug.c: kdp_machine_reboot_type hook')
    write(path, src)
    return report


def main(argv):
    if len(argv) < 3:
        print('usage: nabu-panic-log.py <serial_general.c> <debug.c>')
        return 2
    report = []
    report += patch_console(argv[1])
    report += patch_debug(argv[2])
    print('\n'.join(report))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
