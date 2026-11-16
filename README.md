# UniXNU nabu 无人值守 panic 捕获（UFS 旁路）

目标：内核 panic 后**自动复位回 TWRP**，主机**自动读回 panic 前的日志**，不需要手动按键、不需要拍照。

设备：小米平板 5（nabu / SM8150-AC，12 GiB DRAM），引导链
XBL → ABL → `fastboot boot` Mu UEFI → UniXNU cust payload → XNU。

> ## 定位（2026-11-16 更新）
>
> **本仓库是 panic / USB 死亡场景的旁路方案，不是主线的日志通路。**
>
> 主线的实时日志与控制走 **DWC3 CDC 串口**：见
> [`nabu-usb-cdc`](https://github.com/Lux12138/nabu-usb-cdc)，
> 以及 [`unixnu-nabu`](https://github.com/Lux12138/unixnu-nabu) 的
> `CURRENT-STATE.md` 与 `board-run/`。
>
> 现在的每一轮常规实验都用 CDC 直接抓（单轮 1–10 MB），并配双诊断槽
> （`NABUDIAG seq=0/1`）与全量 os_log 解码。**UFS 窗口只在 USB 功能死亡时**
> 才成为唯一手段——例如 l749 那次上板后 CDC 零字节、adb/fastboot 全不可见，
> 只能靠 UFS 导出区把日志和屏幕快照拉回来。
>
> 仍然有效并继续维护的部分：`NABULOG1` 格式、UFS 窗口几何、misc 预置
> `boot-recovery`、`nabu-panic-restart` 语义、尾部写机制。

## 版本

`patch/nabu-panic-log.py` 现在是 **LOG_VERSION 38**（908 行）。
2026-10-28 那次提交里的版本是 **LOG_VERSION 6**（422 行）。
两者不是小修差别，机制已经换代，见下面「v6 → v38 的实质变化」。

## 现状

| 能力 | 状态 |
|---|---|
| panic 后自动复位 | 已实测（多次自动回到 TWRP，无按键） |
| panic 后自动进 TWRP | 已实测（misc 预置 `boot-recovery`） |
| 主机自动读回日志 | 已实测（读回启动日志并解码） |
| panic 时刻的完整日志 | **放弃 DRAM 方案**（实测跨复位内容损坏），改为周期写盘 |
| panic 尾部落盘 | **只写尾部若干块 + 头**，不再写完整 4 MiB |

## v6 → v38 的实质变化

| 机制 | v6（2026-10-28） | v38（现行） |
|---|---|---|
| panic 时写盘量 | 完整 4 MiB ring + sync | **仅尾部块（默认 24 × 4 KiB = 96 KiB）+ 头**，逐块单块命令，无 sync |
| 写盘时机 | 仅 panic 路径 | **驱动启动一次 + 日志线程每 2 秒 + APFS 挂载读路径** |
| panic 路径安全性 | 直接取 UFS 主机锁（**实测卡死**：屏幕停在 `nabu:flush`，不再复位） | **先查 host lock 是否空闲**，忙则只复位不写 |
| 重复写抑制 | 无（每次触发重写整个尾部，实测占全部命令的 68.5%） | **`total == flushed` 时短路返回**，不花命令；panic 尾部例外，永不短路 |
| 窗口停止位置的语义 | 无法区分「停在哪」 | 明确记录，用于判断挂死点 |
| 尾部块数可调 | 无 | `nabu-paniclog-blocks=1..64`（默认 24） |
| iBoot 内嵌 panic 记录 | 无处理 | **新增 `eph_magic` 判断**，识别 iBoot 未抽取上次崩溃记录的情况 |
| 复位看门狗 | panic 路径 PSCI | 新增 `nabu_log_service_4k()` 弱符号：按输出量续命，让复位跟随证据而非墙钟 |

## 为什么不能直接在 panic 里写 UFS

本移植的 `PE_halt_restart` 为 NULL，`PEHaltRestart()` 不产生复位。
第一次尝试在 panic 路径里 flush 控制台 ring 到 UFS：屏幕上停在 `nabu:flush`，
板子不再复位——panic 上下文（其他核已 halt、中断关闭）里取 UFS 主机锁不会返回。

所以拆成互不阻塞的路径：

1. **正常运行期**：日志线程每 2 秒把 ring 尾部落到 userdata 末尾的诊断窗口。
2. **APFS 挂载期**：读路径也落一次尾部——挂载可能在一个 2 秒间隔内崩掉，
   只靠定时写无法说明它停在哪。
3. **panic 时**：只写尾部块 + 头（`nabu_log_flush_tail_nosync(1)`），
   且仅在 host lock 空闲时；随后 PSCI `SYSTEM_RESET`（`x0=0x84000009`）。
4. **DRAM 暂存已放弃**：曾把 ring 复制到 `0x130000000`（高于 `maxmem`）再复位，
   实测块确实跨复位存活但内容损坏（`Kext` 变成 `Ke2Nel`，header 的 dropped 变成
   `0x04000000`），恢复只会把好日志覆盖成垃圾。代码保留在
   `nabu-panic-restart=2` 下作为实验，默认不启用。
5. **复位后**由 misc 引导进 TWRP，主机侧等 adb `recovery`，`dd` 出窗口、`adb pull`。

## 板级事实（实测）

- GPT 使用 4096 字节 LBA（`logical_block_size=4096`）。
- userdata：`0x288040 .. 0x769E7FA`（464.4 GiB）。
- 日志窗口：userdata 最后 32 MiB = 8192 个 4 KiB 块，绝对 LBA `121,718,715`。
- 窗口下方 256 MiB 是 export 区（`kExportBlocks=65536`），持久 store 不得与两者重叠。
- misc：`0x400 .. 0x7FF`，首块是 Android `bootloader_message`（`command[32]` 在偏移 0）。
- DRAM 暂存：`0x130000000`，8 MiB，内核不使用（`maxmem` 之上）——**仅存档，默认不用**。

## 内核注入点

`patch/nabu-panic-log.py`（由 `scripts/build-xnu-4k-baseline.sh` 调用）：

- `osfmk/console/serial_general.c`
  - `console_printbuf_putc()` 中镜像 `nabu_log_putc(ch)`；
  - 末尾追加 LOG_BLOCK：4 MiB ring、`nabu_log_flush_tail_nosync()`、
    `nabu_panic_save/restore`、`nabu_log_periodic()`、`nabu_psci_reset()`。
- `osfmk/kern/debug.c`
  - `panic()` 入口的无限 `nop` 改为 `nabu-panic-hold=1` 时才停（默认继续走 panic 流程）；
  - `debugger_collect_diagnostics()` 的 panic 分支在 `PEHaltRestart()` **之前**
    调用 `nabu_panic_restart()`（PE 后端为 NULL，不能等它）；
  - `nabu_panic_restart()`：查 host lock → 落尾部 → PSCI 复位。
- `osfmk/arm/nabu_early_mark.c`（构建脚本生成的 heredoc）
  - `nabu_mark_putc()` 中调用 `nabu_log_putc()`。`PE_kputc` 在 `PE_init_kprintf()`
    后指向这里，是 kprintf 的真正 sink；早期 `printf` 走
    `console_printbuf_putc`，两条都覆盖。
- `drivers/NativeUfs/NativeUfs.cpp`
  - root bring-up 成功后：打开窗口、落首次尾部、写 misc `boot-recovery`、
    启动 2 秒周期写线程；读路径插入尾部写。
- `drivers/NativeUfs/RootLayout.h` / `RootCommands.h`
  - 解析 GPT 里的 `misc` 首 LBA；单块写窗口，写入前做范围检查。
- `scripts/make-nabu-adt.py`
  - boot-args 保留 `sheng-log=1 nabu-panic-restart=2`。

`nabu-panic-restart`：`0` 关，`1` 只复位，`2`（默认）先存 DRAM 再复位。
`nabu-paniclog-blocks`：尾部块数，`1..64`，默认 `24`。

## 数据格式

`NABULOG1`（UFS 窗口，小端）：

```
0   8  "NABULOG1"
8   4  version (1)
12  4  flags (bit0 = 来自 panic 路径)
16  4  bytes（头部之后跟随的字节数）
20  4  dropped（ring 绕回后丢弃的字节）
24  8  total（累计捕获字节，低 32 位 + 高 32 位）
4096.. 线性化后的 ring 尾部
```

`NABUSAV1`（DRAM 暂存，**已实测不可用，仅存档**）：

```
0   8  "NABUSAV1"
8   8  total
16  4  head
20  4  dropped
4096.. ring（4 MiB）
```

## 主机侧

- `host/nabu-log-decode.py`（74 行）：离线解码窗口文件，解 `NABULOG1` 头并打印可疑行。
  与板侧 `E:\nabu\tools\` 版本逐字节相同。
- `host/nabu-unattended.py`（277 行）：完整无人值守回路。
  等 fastboot → `fastboot flash cust`（仅 cust）→ `fastboot boot` →
  等设备离开再回来 → `dd` 出 32 MiB 窗口 → `adb pull` → 解 `NABULOG1`。
  `--no-boot` 只读取不启动。
- `host/build_verify.sh`（149 行）与 `host/pack_from_board.sh`（51 行）：
  早期本机增量构建与打包脚本，**已不再对应现行构建流程**
  （现行流程是 `unixnu tree/build/kernel/prelink/payload` + 同路径 bind mount，
  见 `unixnu-nabu` 的构建段）。保留仅作历史参考。

```bash
python host/nabu-unattended.py \
  --cust <cust payload> \
  --kernel <Mu-UEFI 引导镜像> \
  --timeout 240
```

只读取不启动：

```bash
python host/nabu-unattended.py --no-boot
```

## 已知限制

- **DRAM 暂存不可用**：跨 PSCI `SYSTEM_RESET` 后内容损坏，日志以周期 UFS 副本为准。
- panic 那一刻最后 ≤2 秒输出可能丢失；panic 屏上的标记仍需拍照补充。
- 复位后能否进 TWRP 取决于 misc 里的 `boot-recovery`；内核每次启动时写入，
  TWRP 启动后可能清除。
- TWRP 的 adb 状态是 `recovery`（不是 `device`）。
- TWRP 启动约 60–120 秒，主机轮询间隔 10 秒。
- fastboot 无法读分区；读日志必须经 adb/TWRP。
- ring 只捕获 kprintf/printf；**`os_log` 不经过 ring**（os_log 走独立的导出路径）。
- 本仓库不覆盖 `NABUDIAG` 双诊断槽；那是板侧 UFS 导出机制，见 `unixnu-nabu` 的
  `board-run/` 与板侧 `HANDOVER`。
