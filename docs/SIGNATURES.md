# Signature registry

Each detector is a small function that, given a candidate partition start,
re-reads the device, confirms the magic, and returns the volume's own size.
Detectors live in `partrevive.py` and are wired into the `SIGNATURES` table:

```python
SIGNATURES = [
    (b"NTFS    ", 3,     _detect_ntfs),    # (magic, byte offset from part start, detector)
    (b"EXFAT   ", 3,     _detect_exfat),
    (b"FAT32   ", 0x52,  _detect_fat),
    (b"FAT16   ", 0x36,  _detect_fat),
    (b"FAT12   ", 0x36,  _detect_fat),
    (b"\x53\xef", 0x438, _detect_ext),      # ext2/3/4 superblock magic 0xEF53
    (b"SWAPSPACE2", 4086, _detect_swap),
    (b"SWAP-SPACE", 4086, _detect_swap),
    (b"LUKS\xba\xbe", 0,  _detect_luks),    # LUKS (report-only)
    (b"LABELONE",   0,    _detect_lvm),     # LVM2 PV (report-only)
]
```

**Report-only types.** LUKS and LVM2 PVs are detected and *flagged* but not
sized or mounted (you can't size them from a single header, and they need
`cryptsetup`/`vgchange` to open). Their detectors set `report_only=True`; the
scanner lets them through regardless of size, `verify()` marks them `flagged`
instead of mount-testing, and `plan()` excludes them from the proposed table.

The byte offset is where the magic sits relative to the partition's first
sector. The scanner uses it to back-compute the start and reject anything that
isn't sector-aligned, which kills almost all false positives (e.g. the 2-byte
ext magic is only accepted at `offset % 512 == 56`).

## Size fields used

| fstype | size source |
|--------|-------------|
| NTFS   | boot sector `total_sectors` @ 0x28 (+1 for backup boot sector) |
| exFAT  | `VolumeLength` @ 0x48 |
| FAT    | `BPB_TotSec16` @ 0x13 or `BPB_TotSec32` @ 0x20 |
| ext2/3/4 | `s_blocks_count` × (block_size / 512) |
| swap   | `(last_page + 1)` × (pagesize / 512) |

## Adding a filesystem

1. Write `_detect_xxx(dev, part_start_byte)` returning a `Candidate` (or `None`).
   Read the header, confirm the magic, compute `size` in 512-byte sectors.
2. Add `(magic, offset, _detect_xxx)` to `SIGNATURES`.
3. If it needs a non-`0700` GPT type code, set `typecode` in the detector or
   refine it in `_classify()`.

Good next candidates: btrfs (magic `_BHRfS_M` @ 0x10040), XFS (`XFSB` @ 0),
F2FS (`\x10\x20\xf5\xf2` @ 0x400). LVM2 and LUKS are already detected (report-only).
