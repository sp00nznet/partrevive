# Roadmap

Ideas, roughly in priority order. Nothing here is committed.

## Near term
- **MBR output** (`--mbr`) in addition to GPT.
- **More filesystems**: btrfs, XFS, F2FS, LVM2 PV, LUKS, HFS+/APFS detection.
- **Image mode**: `partrevive image /dev/sdX out.img` wrapping `ddrescue`
  (with a mapfile), then recover against the image — ties into salvaging flaky
  pulls without stressing the patient drive.
- **SMART preflight**: warn (and require `--force`) when the target reports
  failing health before any write.

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
