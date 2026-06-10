# Safety model

partrevive is built so that everything is reversible up until one explicit step.

## What writes, and what doesn't

| Command | Writes to disk? |
|---------|-----------------|
| `scan`    | no — raw reads only |
| `verify`  | no — read-only loop devices + `mount -o ro` |
| `plan`    | no — pure computation on scan results |
| `restore` | **yes** — writes the GPT (sectors 1–33 + backup at end) |
| `auto`    | **yes** — runs the pipeline through `restore` |

A `restore` never touches filesystem data. It only writes the 34-sector primary
GPT and its backup. Your files are not moved or rewritten.

## Before it writes

- It prints the proposed table and waits for `y` (unless `-y/--yes`).
- It refuses if the target device (or any partition) is mounted.
- It saves the current table first:
  `--backup-dir/<dev>-gpt-<timestamp>.bin` via `sgdisk --backup`.

## Undo

```bash
sgdisk --load-backup=<dev>-gpt-<timestamp>.bin /dev/sdX
partprobe /dev/sdX
```

Because only the table changed, restoring the backup returns the disk to its
exact prior state.

## Recommended workflow for risky drives

1. If the drive is flaky, image it first with `ddrescue` to a file and run
   partrevive against the image (`losetup` the image, point partrevive at the
   loop device). Recover on the copy, never the patient.
2. Run `verify` and read the live/ghost classification before `restore`.
3. After `restore`, mount the real partition nodes **read-only** and confirm
   your data before trusting the disk.

## Logging

Every invocation appends a header and the per-candidate findings to
`partrevive.log` in the working directory.
