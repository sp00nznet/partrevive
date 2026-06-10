# Worked example: recovering a deleted GPT on a 240 GB SSD

The drive (an OCZ Agility3, `/dev/sdb` over a USB-SATA bridge) had its partition
table deleted. TestDisk could *see* the old partitions but stalled asking for
geometry, because the disk had **two histories layered on it**: a current
Windows install on top of an older, partly-overwritten Linux install.

## What was on the disk

TestDisk's deep search reported overlapping candidates:

```
HPFS-NTFS  start 2048       size 921600      [Recovery]      450 MiB
FAT32      start 923648     size 204800      [EFI System]    100 MiB
HPFS-NTFS  start 1161216    size 467697664   239 GB
Linux ext4 start 21313536   size 16971776    8.7 GB     <- inside the NTFS range
Linux ext4 start ...        size 192024576   91 GB      <- overlaps too
Linux swap start 402911232  size 65949696    31 GB
```

The ext4/swap entries start *inside* the NTFS partition — they can't coexist
with it. One layout is live, the others are ghosts.

## Resolving it by hand (what partrevive automates)

Using TestDisk's own non-destructive `dmsetup` hint, each candidate was mapped
read-only and probed:

```
t_ntfs     -> mounts: Windows 10, 141 GB used, Users/, Program Files/  ✅ LIVE
t_efi      -> mounts: /EFI/{Boot,Microsoft,debian,ubuntu}             ✅ LIVE (ESP)
t_recovery -> mounts: WinRE Recovery                                  ✅ LIVE
t_ext4a    -> mount fails: "Structure needs cleaning"                 ❌ ghost
t_ext4big  -> no fs signature at all                                  ❌ overwritten
```

Verdict: the **Windows GPT** is live; the Linux partitions are dead remnants.

## The write

The NTFS volume's own size field said `467,696,815` sectors, which fits inside
the `467,697,664`-sector slot — no truncation. After backing up the (empty)
table, the GPT was rewritten:

```
1   2048        923647     450.0 MiB   2700  Recovery
2   923648      1128447    100.0 MiB   EF00  EFI system partition
3   1128448     1161215    16.0 MiB    0C01  Microsoft reserved   <- reconstructed gap
4   1161216     468858879  223.0 GiB   0700  Basic data partition
```

Re-reading and mounting the real `/dev/sdb4` confirmed: full Windows tree,
`Users/nedch`, `Windows/System32`, 141 GB of data intact.

## The same thing with partrevive

```bash
sudo ./partrevive.py verify /dev/sdb     # shows the live/ghost split above
sudo ./partrevive.py auto   /dev/sdb     # rebuilds Recovery+ESP+MSR+NTFS, prompts, writes
```

`auto` drops the ext4/swap ghosts automatically, reconstructs the MSR gap
between the ESP and NTFS, and backs up the existing table before writing.
