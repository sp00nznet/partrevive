# Roadmap

Ideas, roughly in priority order. Nothing here is committed.

## Done
- **rescue** — copy files out of every live partition (source stays read-only).
- **undo** — roll a disk back to any saved-table backup (GPT `.bin` or MBR `.sfdisk`).
- **SMART preflight** — `restore`/`auto` refuse on a failing drive unless `--force`.
- **Image input** — pass a disk-image file; it's auto-attached as a loop device.
- **LVM2 / LUKS detection** — flagged in the scan (report-only).
- **MBR output** (`--mbr`) in addition to GPT, with matching undo.
- **btrfs / XFS / F2FS** detectors (real sizes; primary-superblock only).
- **`ddrescue` wrapper** — `partrevive image /dev/sdX out.img`, two-pass + mapfile.

## Near term
- **More filesystems**: HFS+/APFS detection (with real sizes).
- **`--prefer windows|linux|largest`** non-interactive overlap policy for batch runs.

## Medium term
- **Sector-size awareness for EMC/enterprise drives**: detect 520/528-byte
  formatting and offer to drive `sg_format --format --size=512` (today it only
  warns).
- **Confidence scoring**: weight live candidates by file count, presence of
  expected OS markers, and filesystem dirty state, surfaced in `plan`.
- **`--rebuild-bootsector`**: restore an NTFS/FAT boot sector from its backup
  when the primary is damaged.
- **Non-interactive policies**: `--prefer windows|linux|largest|newest` for
  resolving ambiguous overlaps in scripted runs.

## Longer term / separate tools
- **File carving** (a PhotoRec equivalent) for disks with no recoverable
  filesystem structure — likely its own binary.
- **Drive-salvage suite**: secure-erase/unfreeze helpers, controller/firmware
  reset notes per controller, batch processing of a pile of pulls.
- **JSON report + HTML summary** per recovered drive for record-keeping.
