# UniXNU nabu 无人值守 panic 捕获

目标：内核 panic 后**自动复位回 TWRP**，主机**自动读回 panic 前的日志**，不需要手动按键、不需要拍照。

设备：小米平板 5（nabu / SM8150-AC，12 GiB DRAM），引导链 XBL → ABL → `fastboot boot` Mu UEFI → UniXNU cust payload → XNU。

> **定位（2026-10-28 更新）：本仓库是 panic 场景的旁路方案，不是主线的日志通路。**
>
> 主线的实时日志与控制走 **DWC3 CDC 串口 + 无人值守流水线**：见
> [`nabu-usb-cdc`](https://github.com/Lux12138/nabu-usb-cdc)（`docs/07`），
> 以及 [`unixnu-nabu/CURRENT-STATE.md`](https://github.com/Lux12138/unixnu-nabu/blob/main/CURRENT-STATE.md)。
>
> - **仍然有效**：UFS 窗口与 `NABULOG1` 格式、2 秒周期写盘、`nabu-panic-restart` 开关
>   （内核 `nabu-autolog=1` 仍会启用）、misc 预置 `boot-recovery`。
> - **已被取代**：把“读回启动日志”当作主要手段。正常轮次现在直接走 CDC，主机侧不需要
>   `dd` 窗口；复位改由 APSS 硬件狗负责（`nabu-park-after` 定时武装，武装后约 32 s 咬合），
>   不再依赖 panic 路径里的 PSCI `SYSTEM_RESET`（后者只在 panic 当口使用）。


## 现状

| 能力 | 状态 |
|---|---|
| panic 后自动复位 | 已实测（多次自动回到 TWRP，无按键） |
| panic 后自动进 TWRP | 已实测（misc 预置 `boot-recovery`） |
| 主机自动读回日志 | 已实测（读回 57,340 字节启动日志并解码） |
| panic 时刻的完整日志 | **放弃 DRAM 方案**（实测跨复位内容损坏），改为周期写盘，最多丢 2 秒 |

## 为什么不能直接在 panic 里写 UFS

本移植的 `PE_halt_restart` 为 NULL，`PEHaltRestart()` 不产生复位。
第一次尝试在 panic 路径里 flush 控制台 ring 到 UFS：屏幕上停在 `nabu:flush`，板子不再复位——panic 上下文（其他核已 halt、中断关闭）里取 UFS 主机锁不会返回。

所以拆成三条互不阻塞的路径：

1. **正常运行期**：UFS 驱动的内核线程每 2 秒把 4 MiB 控制台 ring 写到 userdata 尾部 32 MiB。
2. **panic 时**：只发 `smc #0` PSCI `SYSTEM_RESET`（`x0=0x84000009`），不碰 UFS、不分配内存。
   曾尝试把 ring 复制到 `0x130000000`（高于 `maxmem`）再复位，实测**不可用**：块确实跨复位存活，
   但内容部分损坏（`Kext` 变成 `Ke2Nel`，header 的 dropped 变成 `0x04000000`），恢复只会把好日志覆盖成垃圾。
   代码保留在 `nabu-panic-restart=2` 下作为实验，默认不再启用。
3. **panic 前后**：运行期每 2 秒写窗口，所以盘上最多丢 2 秒；复位后由 misc 引导进 TWRP。

主机侧等 TWRP 的 adb，`dd` 出窗口、`adb pull`、解 `NABULOG1`。

## 板级事实（实测）

- GPT 使用 4096 字节 LBA（`logical_block_size=4096`）。
- userdata：`0x288040 .. 0x769E7FA`（464.4 GiB）。
- 日志窗口：userdata 最后 32 MiB = 8192 个 4 KiB 块，绝对 LBA `121,718,715`。
- 窗口下方 256 MiB 是 export 区（`kExportBlocks=65536`），持久 store 不得与两者重叠。
- misc：`0x400 .. 0x7FF`，首块是 Android `bootloader_message`（`command[32]` 在偏移 0）。
- DRAM 暂存：`0x130000000`，8 MiB，内核不使用（`maxmem` 之上）。

## 内核注入点

`patch/nabu-panic-log.py`（由 `scripts/build-xnu-4k-baseline.sh` 调用）：

- `osfmk/console/serial_general.c`
  - `console_printbuf_putc()` 中镜像 `nabu_log_putc(ch)`；
  - 末尾追加 LOG_BLOCK：4 MiB ring、`nabu_log_flush()`、`nabu_panic_save/restore`、`nabu_log_periodic()`、`nabu_psci_reset()`。
- `osfmk/kern/debug.c`
  - `panic()` 入口的无限 `nop` 改为 `nabu-panic-hold=1` 时才停（默认继续走 panic 流程）；
  - `debugger_collect_diagnostics()` 的 panic 分支在 `PEHaltRestart()` **之前**调用 `nabu_panic_restart()`（PE 后端为 NULL，不能等它）；
  - `nabu_panic_restart()`：`nabu_panic_save()` → `nabu_psci_reset()`。
- `osfmk/arm/nabu_early_mark.c`（构建脚本生成的 heredoc）
  - `nabu_mark_putc()` 中调用 `nabu_log_putc()`。`PE_kputc` 在 `PE_init_kprintf()` 后指向这里，是 kprintf 的真正 sink；早期 `printf` 走 `console_printbuf_putc`，两条都覆盖。
- `drivers/NativeUfs/NativeUfs.cpp`
  - root bring-up 成功后：映射 DRAM 暂存区、`nabu_log_boot_probe()`（恢复 panic 日志或写当前 ring）、写 misc `boot-recovery`、启动 2 秒周期写线程。
- `drivers/NativeUfs/RootLayout.h` / `RootCommands.h`
  - 解析 GPT 里的 `misc` 首 LBA；增加一个单块写窗口，写入前做范围检查。
- `scripts/make-nabu-adt.py`
  - boot-args 保留 `sheng-log=1 nabu-panic-restart=2`。

`nabu-panic-restart`：`0` 关，`1` 只复位，`2`（默认）先存 DRAM 再复位。

## 数据格式

`NABULOG1`（UFS 窗口，小端）：

```
0   8  "NABULOG1"
8   4  version (1)
12  4  flags (bit0 = 来自 panic)
16  4  bytes
20  4  dropped
24  8  total
4096.. 线性化后的 ring 数据
```

`NABUSAV1`（DRAM 暂存）：

```
0   8  "NABUSAV1"
8   8  total
16  4  head
20  4  dropped
4096.. ring（4 MiB）
```

## 主机侧

`host/nabu-unattended.py`：

1. 等 fastboot（10 秒轮询）。
2. `fastboot flash cust <payload>`，`fastboot boot <loader>`。
3. 等设备离开再回来（fastboot 或 adb `recovery`），10 秒轮询。
4. 设备内 `dd` 出 32 MiB 窗口到 `/tmp`，`adb pull`（exec-out 直传会卡）。
5. 解 `NABULOG1`，打印可疑行。

`host/nabu-log-decode.py`：离线解码窗口文件。

`host/build_verify.sh`、`host/pack_from_board.sh`：本机增量构建与打包（含设备仓库路径，按需修改）。

## 使用

```bash
python host/nabu-unattended.py \
  --cust C:/Temp/cust-final2.img \
  --kernel <Mu-UEFI 引导镜像> \
  --timeout 240
```

只读取不启动：

```bash
python host/nabu-unattended.py --no-boot
```

## 已知限制

- **DRAM 暂存不可用**：PSCI `SYSTEM_RESET` 后该块只有部分内容保留，实测出现位翻转；日志以每 2 秒的 UFS 副本为准。
- 因此 panic 那一刻的最后 ≤2 秒输出可能丢失，包括 panic 字符串本身；panic 屏上的标记仍需拍照补充。
- 复位后能否进 TWRP 取决于 misc 里的 `boot-recovery`；它由内核在每次启动时写入，TWRP 启动后可能清除。
- TWRP 的 adb 状态是 `recovery`（不是 `device`）。
- TWRP 启动约 60–120 秒，主机轮询间隔 10 秒。
- fastboot 无法读分区；读日志必须经 adb/TWRP。
- ring 只捕获 kprintf/printf；`os_log` 不经过 ring。
