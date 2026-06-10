#!/usr/bin/env python3
"""
partrevive — recover lost/deleted partition tables by scanning for filesystem
signatures, verifying each candidate read-only, then rewriting a clean GPT.

A more automated take on TestDisk's "find lost partitions" workflow. The core
idea: never trust a signature blindly. For every candidate partition we build a
read-only view (losetup) and actually try to mount it. Live filesystems with
real data win; stale superblocks left behind by an earlier install ("ghosts")
are dropped. Then each filesystem's *own* size field decides the partition end,
so nothing gets truncated.

Read-only by default. The only operation that writes to the disk is `restore`
(and `auto` once you confirm) — and it backs up the existing table first.

Requires root. Depends on: sgdisk, blkid, losetup, mount, partprobe.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict

SECTOR = 512
MIN_PART_SECTORS = 2048            # ignore sub-1MiB "partitions" (noise)
CHUNK = 16 * 1024 * 1024           # sweep read size
OVERLAP = 1 * 1024 * 1024          # re-read window so signatures don't straddle chunks
LOGFILE = "partrevive.log"

# ---- terminal helpers -------------------------------------------------------

def _c(code: str, s: str) -> str:
    return s if os.environ.get("NO_COLOR") else f"\033[{code}m{s}\033[0m"

def bold(s): return _c("1", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def red(s): return _c("31", s)
def dim(s): return _c("2", s)

_logfh = None
_sink = None

def set_log_sink(fn):
    """Register a callback that receives every log line (used by the GUI)."""
    global _sink
    _sink = fn

def log(msg: str, *, quiet_console=False):
    global _logfh
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    if _logfh:
        _logfh.write(line + "\n"); _logfh.flush()
    if not quiet_console:
        print(msg)
    if _sink:
        try:
            _sink(msg)
        except Exception:
            pass


def list_disks() -> list[dict]:
    """Whole disks (no partitions/loops) for a picker: name, size, model, tran."""
    out = run(["lsblk", "-dn", "-o", "NAME,SIZE,TYPE,TRAN,MODEL"], check=False).stdout
    disks = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 3 and parts[2] == "disk" and not parts[0].startswith("loop"):
            disks.append({"dev": f"/dev/{parts[0]}", "size": parts[1],
                          "tran": parts[3] if len(parts) > 3 else "",
                          "model": parts[4] if len(parts) > 4 else ""})
    return disks

def die(msg: str, code: int = 1):
    log(red("error: ") + msg)
    sys.exit(code)


def run(cmd, *, check=True, capture=True):
    return subprocess.run(cmd, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None,
                          text=True)

# ---- low-level device reads -------------------------------------------------

def dev_size_sectors(dev: str) -> int:
    return int(run(["blockdev", "--getsz", dev]).stdout.strip())

def logical_sector_size(dev: str) -> int:
    try:
        return int(run(["blockdev", "--getss", dev]).stdout.strip())
    except Exception:
        return SECTOR

def read_at(dev: str, byte_off: int, length: int) -> bytes:
    with open(dev, "rb", buffering=0) as f:
        f.seek(byte_off)
        return f.read(length)

# ---- signature registry -----------------------------------------------------
# Each detector locates a magic string somewhere inside a filesystem's header,
# back-computes where the partition would have to start (must be sector-aligned),
# then re-reads from the device to fully validate and pull the volume's own size.

@dataclass
class Candidate:
    start: int                 # LBA (sectors)
    size: int                  # sectors, from the filesystem's own metadata
    fstype: str
    label: str = ""
    typecode: str = "0700"     # sgdisk type; refined during verify
    confidence: str = "signature"   # signature | mounted | ghost | flagged
    note: str = ""
    report_only: bool = False  # detected but not sizeable/mountable (LVM/LUKS)
    # populated by verify():
    blkid: str = ""
    mount_ok: bool = False
    entries: list = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.start + self.size - 1


def _u(b: bytes) -> int:           # little-endian unsigned
    return int.from_bytes(b, "little")


def _has_jump(bs: bytes) -> bool:
    # real FAT/NTFS/exFAT boot sectors begin with a jump: EB xx 90 or E9 xx xx
    return bs[0] == 0xEB or bs[0] == 0xE9


def _detect_ntfs(dev, part_start_byte):
    bs = read_at(dev, part_start_byte, SECTOR)
    if bs[3:11] != b"NTFS    " or bs[510:512] != b"\x55\xaa" or not _has_jump(bs):
        return None
    total = _u(bs[0x28:0x30])      # excludes the backup boot sector
    if total <= 0 or total > (1 << 40):
        return None
    # Recovery partitions are small and carry a WinRE; size guess refined later.
    return Candidate(part_start_byte // SECTOR, total + 1, "ntfs", typecode="0700")


def _detect_exfat(dev, part_start_byte):
    bs = read_at(dev, part_start_byte, SECTOR)
    if bs[3:11] != b"EXFAT   " or bs[510:512] != b"\x55\xaa" or not _has_jump(bs):
        return None
    vol_len = _u(bs[0x48:0x50])     # sectors
    if vol_len <= 0:
        return None
    return Candidate(part_start_byte // SECTOR, vol_len, "exfat", typecode="0700")


def _detect_fat(dev, part_start_byte):
    bs = read_at(dev, part_start_byte, SECTOR)
    if bs[510:512] != b"\x55\xaa" or not _has_jump(bs):
        return None
    is32 = bs[0x52:0x57] == b"FAT32"
    is16 = bs[0x36:0x3b] in (b"FAT16", b"FAT12", b"FAT  ")
    if not (is32 or is16):
        return None
    tot16 = _u(bs[0x13:0x15])
    tot32 = _u(bs[0x20:0x24])
    total = tot16 if tot16 else tot32
    if total <= 0:
        return None
    return Candidate(part_start_byte // SECTOR, total, "vfat", typecode="0700")


def _detect_ext(dev, part_start_byte):
    sb = read_at(dev, part_start_byte + 1024, 1024)   # ext superblock @ +1024
    if sb[0x38:0x3a] != b"\x53\xef":                  # s_magic 0xEF53
        return None
    if _u(sb[0x5a:0x5c]) != 0:                        # s_block_group_nr: 0 = primary, else a backup
        return None
    blocks = _u(sb[0x04:0x08])
    log_bs = _u(sb[0x18:0x1c])
    block_size = 1024 << log_bs
    if blocks <= 0 or block_size <= 0:
        return None
    size_sectors = blocks * (block_size // SECTOR)
    label = sb[0x78:0x88].split(b"\x00", 1)[0].decode("latin1", "replace")
    return Candidate(part_start_byte // SECTOR, size_sectors, "ext4",
                     label=label, typecode="8300")


def _detect_swap(dev, part_start_byte):
    page = read_at(dev, part_start_byte, 4096)
    if page[4086:4096] not in (b"SWAPSPACE2", b"SWAP-SPACE"):
        return None
    last_page = _u(page[0x408:0x40c])                 # header: bootbits[1024]+version+last_page
    if last_page <= 0:
        return None
    size_sectors = (last_page + 1) * (4096 // SECTOR)
    return Candidate(part_start_byte // SECTOR, size_sectors, "swap", typecode="8200")


def _detect_luks(dev, part_start_byte):
    bs = read_at(dev, part_start_byte, 8)
    if bs[0:6] != b"LUKS\xba\xbe":          # LUKS1 & LUKS2 both start here
        return None
    ver = int.from_bytes(bs[6:8], "big")    # version is big-endian
    return Candidate(part_start_byte // SECTOR, MIN_PART_SECTORS, "crypto_LUKS",
                     typecode="8309", report_only=True,
                     note=f"LUKS{ver or '?'} encrypted — unlock to recover (cryptsetup)")


def _detect_lvm(dev, part_start_byte_of_label):
    # The LVM2 PV label ("LABELONE") sits in one of the PV's first 4 sectors; its
    # own header records which sector, so we can back-compute the PV start.
    lh = read_at(dev, part_start_byte_of_label, 512)
    if lh[0:8] != b"LABELONE" or lh[0x20:0x28] != b"LVM2 001":
        return None
    label_sector = _u(lh[8:16])
    start = part_start_byte_of_label - label_sector * SECTOR
    if start < 0 or start % SECTOR != 0:
        return None
    return Candidate(start // SECTOR, MIN_PART_SECTORS, "LVM2_member",
                     typecode="8E00", report_only=True,
                     note="LVM physical volume — activate with vgscan/vgchange to recover")


# magic string, its byte offset from partition start, detector, alignment(hit%512)
SIGNATURES = [
    (b"NTFS    ", 3,     _detect_ntfs),
    (b"EXFAT   ", 3,     _detect_exfat),
    (b"FAT32   ", 0x52,  _detect_fat),
    (b"FAT16   ", 0x36,  _detect_fat),
    (b"FAT12   ", 0x36,  _detect_fat),
    (b"\x53\xef", 0x438, _detect_ext),       # ext2/3/4
    (b"SWAPSPACE2", 4086, _detect_swap),
    (b"SWAP-SPACE", 4086, _detect_swap),
    (b"LUKS\xba\xbe", 0,  _detect_luks),     # LUKS (report-only)
    (b"LABELONE",   0,    _detect_lvm),      # LVM2 PV (report-only)
]

# ---- scan -------------------------------------------------------------------

def scan(dev: str, *, deep: bool, progress=True, on_progress=None, on_found=None) -> list[Candidate]:
    total_sectors = dev_size_sectors(dev)
    total = total_sectors * SECTOR
    found: dict[int, Candidate] = {}     # keyed by start sector, first hit wins
    log(dim(f"scanning {dev} ({total/1e9:.1f} GB){' [deep]' if deep else ''} ..."))
    pos = 0
    last_pct = -1
    with open(dev, "rb", buffering=0) as f:
        while pos < total:
            f.seek(pos)
            buf = f.read(CHUNK + OVERLAP)
            if not buf:
                break
            for magic, moff, detector in SIGNATURES:
                idx = 0
                while True:
                    i = buf.find(magic, idx)
                    if i < 0:
                        break
                    idx = i + 1
                    part_byte = pos + i - moff
                    if part_byte < 0 or part_byte % SECTOR != 0:
                        continue
                    if part_byte // SECTOR in found:
                        continue
                    try:
                        cand = detector(dev, part_byte)
                    except OSError:
                        cand = None
                    if (cand and MIN_PART_SECTORS <= cand.size
                            and cand.start + cand.size <= total_sectors):
                        found[cand.start] = cand
                        log(dim(f"  + {cand.fstype:6} @ sector {cand.start} "
                                f"({cand.size*SECTOR/1e9:.2f} GB)"
                                f"{(' '+cand.label) if cand.label else ''}"),
                            quiet_console=True)
                        if on_found:
                            on_found(cand)
            pos += CHUNK
            if total:
                pct = min(100, int(pos * 100 / total))
                if pct != last_pct:
                    if progress:
                        print(f"\r  scan {pct:3d}%", end="", flush=True)
                    if on_progress:
                        on_progress(pct)
                    last_pct = pct
            if not deep:
                # quick mode: filesystem boot sectors live at the partition's
                # first sectors; a stride sweep still reads everything since
                # find() is content-based, so "quick" just means single pass.
                pass
    if progress:
        print("\r" + " " * 20 + "\r", end="")
    return sorted(found.values(), key=lambda c: c.start)

# ---- verify (read-only) -----------------------------------------------------

def _losetup(dev, start_sector, size_sectors) -> str:
    out = run(["losetup", "-r", "-f", "--show",
               "-o", str(start_sector * SECTOR),
               "--sizelimit", str(size_sectors * SECTOR), dev]).stdout.strip()
    return out

def verify(dev: str, cands: list[Candidate], on_result=None) -> list[Candidate]:
    log(bold("verifying candidates read-only (mount test) ..."))
    mnt = tempfile.mkdtemp(prefix="partrevive_")
    try:
        for c in cands:
            if c.report_only:                  # LVM/LUKS: flagged, not mountable here
                c.confidence = "flagged"
                log(f"  [{yellow('flag ')}] {c.fstype:12} @ {c.start:>12}  -> {c.note}")
                if on_result:
                    on_result(c)
                continue
            loop = ""
            try:
                loop = _losetup(dev, c.start, c.size)
                bk = run(["blkid", "-o", "export", loop], check=False).stdout
                c.blkid = " ".join(l for l in bk.split() if l.startswith(("TYPE=", "LABEL=", "UUID=")))
                r = run(["mount", "-o", "ro", loop, mnt], check=False)
                if r.returncode == 0:
                    c.mount_ok = True
                    c.confidence = "mounted"
                    try:
                        c.entries = sorted(os.listdir(mnt))[:40]
                    except OSError:
                        c.entries = []
                    _classify(c)
                    run(["umount", mnt], check=False)
                else:
                    if c.fstype == "swap":
                        c.confidence = "mounted"   # swap can't mount; signature is enough
                        c.note = "swap area"
                    else:
                        c.confidence = "ghost"
                        c.note = (r.stdout or "").strip().splitlines()[-1] if r.stdout else "mount failed"
            except Exception as e:
                c.note = str(e)
            finally:
                if loop:
                    run(["losetup", "-d", loop], check=False)
            tag = green("LIVE ") if c.confidence == "mounted" else (
                  yellow("swap ") if c.fstype == "swap" else red("ghost"))
            log(f"  [{tag}] {c.fstype:6} @ {c.start:>12}  {c.size*SECTOR/1e9:7.2f} GB  "
                f"{c.blkid}{('  -> '+c.note) if c.note else ''}")
            if on_result:
                on_result(c)
    finally:
        shutil.rmtree(mnt, ignore_errors=True)
    return cands


def _classify(c: Candidate):
    """Refine sgdisk type code from what we found inside."""
    names = {e.lower() for e in c.entries}
    if c.fstype == "vfat" and "efi" in names:
        c.typecode = "EF00"; c.note = "EFI System Partition"
    elif c.fstype == "ntfs":
        lbl = ""
        for kv in c.blkid.split():
            if kv.startswith("LABEL="):
                lbl = kv.split("=", 1)[1].strip('"')
        if lbl.lower().startswith("recovery") or "recovery" in names and c.size * SECTOR < 2 * 1024**3:
            c.typecode = "2700"; c.note = "Windows recovery"
        else:
            c.typecode = "0700"
        c.label = lbl or c.label

# ---- plan: resolve overlaps + reconstruct MSR -------------------------------

def plan(cands: list[Candidate], dev: str) -> list[Candidate]:
    live = [c for c in cands if c.confidence == "mounted"]
    # Resolve overlaps: prefer mounted-with-most-data; ghosts already excluded.
    live.sort(key=lambda c: (c.start, -(len(c.entries))))
    chosen: list[Candidate] = []
    for c in live:
        if any(not (c.end < o.start or c.start > o.end) for o in chosen):
            continue   # overlaps an already-chosen live partition
        chosen.append(c)
    chosen.sort(key=lambda c: c.start)

    # Reconstruct a Microsoft Reserved gap between an ESP and the next NTFS/data.
    rebuilt: list[Candidate] = []
    for i, c in enumerate(chosen):
        rebuilt.append(c)
        if c.typecode == "EF00" and i + 1 < len(chosen):
            nxt = chosen[i + 1]
            gap = nxt.start - (c.end + 1)
            if 2048 <= gap <= 64 * 2048:    # ~1MiB..64MiB unallocated => MSR
                rebuilt.append(Candidate(c.end + 1, gap, "msr",
                                         typecode="0C01", confidence="reconstructed",
                                         note="reconstructed Microsoft reserved"))
    return rebuilt

# ---- restore (the only writing operation) -----------------------------------

def restore(dev: str, parts: list[Candidate], *, assume_yes: bool, backup_dir: str, force: bool = False):
    if not parts:
        die("nothing to restore — no live partitions found")
    if _is_mounted(dev):
        die(f"{dev} (or a partition of it) is mounted — unmount before restoring")
    ok, summary = smart_health(dev)
    if ok is False:
        log(red(f"⚠ {summary}"))
        if not force:
            die("drive reports failing health — image it with ddrescue first, "
                "or pass --force to write anyway")
    elif ok is None:
        log(dim(f"({summary})"))
    else:
        log(dim(summary))
    print()
    log(bold(f"proposed GPT for {dev}:"))
    _print_table(parts, dev)
    if not assume_yes:
        ans = input(bold("\nwrite this partition table? ") + "[y/N] ").strip().lower()
        if ans != "y":
            die("aborted by user", code=0)

    backup = write_gpt(dev, parts, backup_dir)
    log(green("partition table written."))
    print()
    log(run(["sgdisk", "-p", dev]).stdout)
    log(dim(f"undo with:  sgdisk --load-backup={backup} {dev}"))


def write_gpt(dev: str, parts: list[Candidate], backup_dir: str) -> str:
    """Back up the current table and write the new GPT. Returns the backup path.
    The single disk-writing primitive; shared by the CLI and the GUI."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(backup_dir, f"{os.path.basename(dev)}-gpt-{stamp}.bin")
    run(["sgdisk", f"--backup={backup}", dev])
    log(green(f"backed up current table -> {backup}"))

    args = ["sgdisk"]
    for i, c in enumerate(parts, 1):
        name = {"EF00": "EFI system partition", "0C01": "Microsoft reserved partition",
                "2700": "Recovery", "8200": "Linux swap", "8300": "Linux filesystem",
                }.get(c.typecode, "Basic data partition")
        if c.label:
            name = c.label
        args += [f"-n", f"{i}:{c.start}:{c.end}", "-t", f"{i}:{c.typecode}", "-c", f"{i}:{name}"]
    args.append(dev)
    run(args)
    run(["partprobe", dev], check=False)
    time.sleep(2)
    return backup

# ---- SMART preflight --------------------------------------------------------

def smart_health(dev: str):
    """Best-effort drive-health check. Returns (ok, summary):
    ok is True (healthy), False (failing), or None (unknown/unavailable)."""
    if not shutil.which("smartctl") or _is_loop_or_image(dev):
        return None, "SMART unavailable"
    # USB bridges usually need an explicit device type; try a few.
    out = ""
    for dtype in (None, "sat", "scsi"):
        cmd = ["smartctl", "-H", "-A"] + (["-d", dtype] if dtype else []) + [dev]
        r = run(cmd, check=False)
        out = r.stdout or ""
        if "self-assessment" in out.lower() or "SMART Health" in out:
            break
    low = out.lower()
    if "failed" in low and "self-assessment" in low:
        return False, "SMART overall-health: FAILED"
    bad = 0
    for line in out.splitlines():
        if any(k in line for k in ("Reallocated_Sector", "Current_Pending_Sector",
                                   "Offline_Uncorrectable", "Reported_Uncorrect")):
            parts = line.split()
            try:
                if int(parts[-1]) > 0:
                    bad += int(parts[-1])
            except ValueError:
                pass
    if bad:
        return False, f"SMART: {bad} reallocated/pending/uncorrectable sectors"
    if "passed" in low:
        return True, "SMART overall-health: PASSED"
    return None, "SMART status indeterminate"


# ---- rescue: copy files out (no writes to the source) -----------------------

def rescue(dev: str, cands: list[Candidate], dest: str) -> dict:
    """Mount each live partition read-only and copy its contents into dest.
    Never writes to the source disk. Returns a per-partition summary."""
    live = [c for c in cands if c.confidence == "mounted" and c.fstype != "swap"]
    if not live:
        die("no live (mountable) partitions to rescue")
    os.makedirs(dest, exist_ok=True)
    mnt = tempfile.mkdtemp(prefix="partrevive_rescue_")
    results = []
    copier = "rsync" if shutil.which("rsync") else "cp"
    try:
        for n, c in enumerate(live, 1):
            label = (c.label or c.fstype).replace("/", "_").strip() or c.fstype
            outdir = os.path.join(dest, f"p{n}_{c.start}_{label}")
            os.makedirs(outdir, exist_ok=True)
            loop = ""
            try:
                loop = _losetup(dev, c.start, c.size)
                if run(["mount", "-o", "ro", loop, mnt], check=False).returncode != 0:
                    results.append((outdir, False, "mount failed")); continue
                log(bold(f"rescuing p{n} ({c.fstype}, {human_sectors(c.size)}) -> {outdir}"))
                if copier == "rsync":
                    r = run(["rsync", "-a", "--info=progress2", "--no-inc-recursive",
                             mnt + "/", outdir + "/"], check=False, capture=False)
                else:
                    r = run(["cp", "-a", mnt + "/.", outdir + "/"], check=False)
                ok = (r.returncode == 0)
                results.append((outdir, ok, "ok" if ok else "copy errors (partial)"))
            finally:
                run(["umount", mnt], check=False)
                if loop:
                    run(["losetup", "-d", loop], check=False)
    finally:
        shutil.rmtree(mnt, ignore_errors=True)
    log(bold("\nrescue summary:"))
    for outdir, ok, msg in results:
        log(("  " + green("✓") if ok else "  " + red("✗")) + f" {outdir}  ({msg})")
    return {"dest": dest, "partitions": results}


# ---- undo: restore a saved table -------------------------------------------

def undo(dev: str, backup_file: str):
    if not os.path.exists(backup_file):
        die(f"backup file not found: {backup_file}")
    if _is_mounted(dev):
        die(f"{dev} is mounted — unmount before restoring a table")
    run(["sgdisk", f"--load-backup={backup_file}", dev])
    run(["partprobe", dev], check=False)
    time.sleep(1)
    log(green(f"restored table on {dev} from {backup_file}"))
    log(run(["sgdisk", "-p", dev]).stdout)

# ---- helpers ----------------------------------------------------------------

def human_sectors(sectors: int) -> str:
    b = sectors * SECTOR
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= div:
            return f"{b/div:.1f} {unit}"
    return f"{b} B"

def _is_mounted(dev: str) -> bool:
    mounts = run(["lsblk", "-no", "MOUNTPOINT", dev], check=False).stdout
    return any(line.strip() for line in mounts.splitlines())

def _is_loop_or_image(dev: str) -> bool:
    return dev.startswith("/dev/loop") or os.path.isfile(dev)


def setup_target(path: str, *, writable: bool):
    """Resolve the target to a block device. If `path` is a regular file (a disk
    image), attach it as a loop device. Returns (dev, loop_to_detach_or_None)."""
    if os.path.isfile(path):
        flags = ["losetup", "-P", "-f", "--show"]
        if not writable:
            flags.insert(1, "-r")
        loop = run(flags + [path]).stdout.strip()
        log(dim(f"attached image {path} -> {loop} ({'rw' if writable else 'ro'})"))
        return loop, loop
    return path, None

def teardown_target(loop):
    if loop:
        run(["losetup", "-d", loop], check=False)

def _print_table(parts: list[Candidate], dev: str):
    print(f"  {'#':<3}{'start':>12}{'end':>14}{'size':>11}  {'code':<6}{'type'}")
    for i, c in enumerate(parts, 1):
        sz = "?" if c.report_only else human_sectors(c.size).replace(" ", "")
        kind = c.note or c.fstype
        flag = green("live") if c.confidence == "mounted" else dim(c.confidence)
        print(f"  {i:<3}{c.start:>12}{c.end:>14}{sz:>11}  {c.typecode:<6}{kind}  [{flag}]")

def _emit_json(dev, cands):
    print(json.dumps({"device": dev,
                      "candidates": [asdict(c) for c in cands]}, indent=2))

# ---- preflight --------------------------------------------------------------

def preflight(dev: str):
    if os.geteuid() != 0:
        die("must run as root")
    if not os.path.exists(dev):
        die(f"{dev} does not exist")
    for tool in ("sgdisk", "blkid", "losetup", "mount", "partprobe", "blockdev"):
        if not shutil.which(tool):
            die(f"required tool not found: {tool}")
    lss = logical_sector_size(dev)
    if lss != SECTOR:
        log(yellow(f"warning: logical sector size is {lss}, not 512. "
                   "Enterprise/EMC drives are often 520/528-byte formatted — "
                   "reformat with `sg_format --format --size=512` before use."))
    # USB bridge note (callback: bridges filter low-level commands)
    tran = run(["lsblk", "-ndo", "TRAN", dev], check=False).stdout.strip()
    if tran == "usb":
        log(dim("note: target is on a USB bridge; fine for partition recovery, "
                "but secure-erase/sanitize/format commands may be filtered — use a "
                "native SATA port for those."))

# ---- CLI --------------------------------------------------------------------

def _open_log(dev):
    global _logfh
    try:
        _logfh = open(LOGFILE, "a")
        _logfh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(sys.argv)} ===\n")
    except OSError:
        _logfh = None

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "gui":
        import partrevive_gui
        partrevive_gui.main()
        return

    ap = argparse.ArgumentParser(prog="partrevive",
        description="Recover a lost/deleted partition table by scanning for "
                    "filesystem signatures and verifying them read-only.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, helptext in [
        ("scan", "find candidate filesystems (read-only)"),
        ("verify", "scan + mount-test each candidate (read-only)"),
        ("plan", "verify + resolve overlaps into a proposed table (read-only)"),
        ("rescue", "copy files out of every live partition (read-only source)"),
        ("restore", "plan + back up current table + write new GPT"),
        ("auto", "scan -> verify -> plan -> restore in one shot"),
        ("undo", "restore a previously saved partition table"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("device", help="block device or a disk image file")
        if name != "undo":
            p.add_argument("--deep", action="store_true", help="full-surface sweep")
        if name == "rescue":
            p.add_argument("--to", required=True, metavar="DIR", help="destination directory")
        if name == "undo":
            p.add_argument("backup", help="the <dev>-gpt-<stamp>.bin backup file")
        if name in ("restore", "auto"):
            p.add_argument("-y", "--yes", action="store_true", help="don't prompt before writing")
            p.add_argument("--force", action="store_true", help="write even if SMART reports failing")
            p.add_argument("--backup-dir", default=".", help="where to save the table backup")

    args = ap.parse_args()
    _open_log(args.device)

    writable = args.cmd in ("restore", "auto", "undo")
    dev, loop = setup_target(args.device, writable=writable)
    try:
        preflight(dev)

        if args.cmd == "undo":
            undo(dev, args.backup)
            return

        cands = scan(dev, deep=getattr(args, "deep", False))
        if not cands:
            die("no filesystem signatures found")

        if args.cmd == "scan":
            if args.json: _emit_json(dev, cands)
            else:
                for c in cands:
                    sz = "    ?   " if c.report_only else f"{c.size*SECTOR/1e9:7.2f}"
                    log(f"  {c.fstype:12} @ {c.start:>12}  {sz} GB  {c.note or c.label}")
            return

        verify(dev, cands)
        if args.cmd == "verify":
            if args.json: _emit_json(dev, cands)
            return

        if args.cmd == "rescue":
            rescue(dev, cands, args.to)
            return

        parts = plan(cands, dev)
        if args.cmd == "plan":
            if args.json: _emit_json(dev, parts)
            else:
                print(); log(bold("proposed table:")); _print_table(parts, dev)
                log(dim("\nrun `partrevive restore` to write it (current table is backed up first)."))
            return

        # restore / auto
        restore(dev, parts, assume_yes=getattr(args, "yes", False),
                backup_dir=getattr(args, "backup_dir", "."), force=getattr(args, "force", False))
    finally:
        teardown_target(loop)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted", code=130)
