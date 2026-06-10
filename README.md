# partrevive

Recover a **lost or deleted partition table** by scanning the raw disk for
filesystem signatures, verifying every candidate **read-only**, and rewriting a
clean GPT — a more automated take on TestDisk's "find lost partitions" flow.

The trick TestDisk leaves to you, partrevive does itself: for each candidate it
builds a read-only view and *actually mounts it*. Filesystems with real data win;
stale superblocks from an earlier install ("ghosts") are discarded. Each
filesystem's **own size field** sets the partition end, so nothing is truncated.

> ⚠️ Recovery tool for disks you own. `restore`/`auto` write a partition table.
> They back up the existing one first, but read the proposed table before saying yes.

## Install

Single file, Python 3, stdlib only. Needs root and these tools on PATH:
`sgdisk blkid losetup mount partprobe blockdev`.

```bash
sudo apt install gdisk util-linux        # provides the above
chmod +x partrevive.py
sudo ./partrevive.py --help
```

## Usage

Five subcommands, each a superset of the previous. Everything up to `restore`
is **read-only**.

```bash
sudo ./partrevive.py scan    /dev/sdX            # list candidate filesystems
sudo ./partrevive.py verify  /dev/sdX            # + mount-test each (live vs ghost)
sudo ./partrevive.py plan    /dev/sdX            # + resolve overlaps -> proposed GPT
sudo ./partrevive.py rescue  /dev/sdX --to DIR   # copy files out (source stays read-only)
sudo ./partrevive.py restore /dev/sdX            # + back up table + write it (prompts)
sudo ./partrevive.py auto    /dev/sdX            # the whole pipeline in one shot
sudo ./partrevive.py undo    /dev/sdX TABLE.bin  # roll back to a saved table
```

The `device` can also be a **disk image file** — partrevive attaches it as a
loop device automatically, so you can `ddrescue` a flaky drive to an image and
recover against the copy:

```bash
sudo ./partrevive.py rescue disk.img --to ~/recovered
```

Useful flags: `--deep` (full-surface sweep), `--json` (machine-readable),
`-y/--yes` (skip the write confirmation), `--force` (write even if SMART reports
failing), `--backup-dir DIR`.

**Safety extras:** before any write, `restore`/`auto` run a SMART health check
and refuse on a failing drive unless you pass `--force` (image it first). The
`undo` command rolls a disk back to any saved-table backup.

Typical session:

```bash
sudo ./partrevive.py verify /dev/sdb      # eyeball what's live
sudo ./partrevive.py auto   /dev/sdb      # let it rebuild, confirm at the prompt
```

### GUI

A Tkinter front-end (same engine, imports `partrevive.py`) walks the pipeline
as a guided flow — pick a disk, **① Scan & Verify** (read-only), **② Build
Plan**, **③ Restore**. Candidates are colour-coded live vs ghost, the proposed
table is shown before any write, and Restore stays disabled until there's a
valid plan and the disk is unmounted.

```bash
sudo apt install python3-tk        # if tkinter isn't present
sudo python3 partrevive_gui.py     # or:  sudo python3 partrevive.py gui
```

### CLI

Example output (a disk with a live Windows layout over dead Linux ghosts):

```
  [LIVE ] ntfs   @         2048     0.47 GB  TYPE="ntfs" LABEL="Recovery"
  [LIVE ] vfat   @       923648     0.10 GB  TYPE="vfat"   -> EFI System Partition
  [ghost] ext4   @     21313536     8.69 GB  TYPE="ext4"   -> mount failed
  [LIVE ] ntfs   @      1161216   239.46 GB  TYPE="ntfs"
```

## Safety model

- Only `restore`/`auto` write, and only the GPT (sectors 1–33 + backup) — never
  filesystem data.
- The existing table is saved to `<dev>-gpt-<timestamp>.bin` first. Undo with
  `sgdisk --load-backup=<file> /dev/sdX`.
- `restore` refuses if the target is mounted.
- Every run appends to `partrevive.log`.

## What it detects

NTFS, FAT12/16/32, exFAT, ext2/3/4, Linux swap — and **flags** LVM2 PVs and
LUKS-encrypted volumes (reported, not rebuilt, since they're not sizeable from a
single header). Reconstructs the Microsoft Reserved (MSR) gap on Windows disks.
See [docs/SIGNATURES.md](docs/SIGNATURES.md).

## More docs

- [docs/DESIGN.md](docs/DESIGN.md) — how it works and how it differs from TestDisk
- [docs/SIGNATURES.md](docs/SIGNATURES.md) — the signature registry & how to extend it
- [docs/SAFETY.md](docs/SAFETY.md) — the safety/undo model in detail
- [docs/ROADMAP.md](docs/ROADMAP.md) — planned features
- [examples/ocz-recovery.md](examples/ocz-recovery.md) — a real worked recovery

## Status

Private / early. Tested recovering a deleted GPT on a 240 GB SATA SSD
(Windows + EFI + Recovery over stale Linux partitions).
