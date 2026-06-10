# Design

## The problem with the manual flow

TestDisk finds lost partitions well, but when filesystems overlap (a disk that
held an old install before the current one), it surfaces every candidate and
asks the operator to resolve geometry/CHS and pick a layout by hand. That last
mile is where it stalls — it expects you to know which candidates are real and
to supply start/end sectors.

## partrevive's approach

Four stages, each strictly read-only until the last:

1. **Scan.** Sweep the raw device in 16 MiB blocks. For each known filesystem,
   `bytes.find()` locates its magic string (C-fast), back-computes the implied
   partition start, and requires it to be sector-aligned. Each hit is re-read
   from the device and fully validated, pulling the volume's **own size** from
   its metadata (NTFS `total_sectors`, ext `s_blocks_count`, FAT `BPB_TotSec`,
   etc.). Content-based search means even "quick" mode reads the whole surface
   once; `--deep` is reserved for future stride/heuristic passes.

2. **Verify.** For every candidate, `losetup -r -o <start> --sizelimit <size>`
   builds a read-only window, then we run `blkid` and actually `mount -o ro`.
   - mounts with data → **live**
   - won't mount → **ghost** (stale superblock overwritten by a later install)
   - swap → accepted on signature (can't be mounted)
   While mounted we peek at the root listing to refine the partition type
   (a vfat with `/EFI` is an ESP; a small NTFS labelled `Recovery` is WinRE).

3. **Plan.** Drop ghosts, resolve overlaps among live partitions (earliest +
   most-populated wins), and reconstruct the Microsoft Reserved gap when an ESP
   is followed by ~16 MiB of unallocated space before the next data partition.

4. **Restore.** Back up the current table with `sgdisk --backup`, then write the
   new GPT with exact start/end sectors and inferred type codes.

## Why read-the-size-from-the-filesystem matters

If you size a partition by guessing (next-candidate-start, or
round-to-disk-end), you risk cutting off the NTFS backup boot sector or
clipping the volume. Reading `total_sectors` from the filesystem header and
giving the partition at least that many sectors guarantees the volume fits.

## Why losetup instead of dmsetup

Both work. `losetup -r -o … --sizelimit …` against the block device gives a
read-only window without naming/teardown bookkeeping, and cleans up with a
single `losetup -d`. (The manual prototype used `dmsetup create … linear`,
which TestDisk also prints as a hint.)

## Limitations

- GPT output only for now (MBR planned).
- btrfs/XFS/LVM/LUKS are not yet in the registry.
- Heavily fragmented or partially-overwritten filesystems may mount but be
  incomplete — verify your data after recovery.
