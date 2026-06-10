#!/usr/bin/env python3
"""partrevive_gui.py - Tkinter GUI for partrevive.

Same engine as the CLI (imports partrevive.py). Walks the recovery flow as a
guided pipeline: pick a disk -> Scan & Verify (read-only) -> Build Plan ->
Restore (writes the GPT, after a confirmation, backing up the old table first).

Everything up to Restore is read-only. Needs root (raw device reads).

Run:  sudo python3 partrevive_gui.py     (or:  sudo python3 partrevive.py gui)
"""
import os, sys, re, threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import partrevive as pr

SECTOR = pr.SECTOR
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# status -> (row tag, swatch colour) for the candidate list
TAGCOLOR = {"mounted": ("live", "#27d0a8"),
            "ghost":   ("ghost", "#d05a5a"),
            "flagged": ("flag", "#c8a13a"),
            "signature": ("sig", "#c8a13a"),
            "reconstructed": ("recon", "#6a9fd0")}


def human(sectors: int) -> str:
    b = sectors * SECTOR
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= div:
            return f"{b/div:.1f} {unit}"
    return f"{b} B"


class PartReviveGUI:
    def __init__(self, root):
        self.root = root
        root.title("partrevive")
        root.minsize(820, 680)
        self.cands = []          # verified candidates
        self.parts = []          # planned partitions
        self.busy = False
        self.dev = tk.StringVar()
        self.deep = tk.BooleanVar(value=False)
        self.mbr = tk.BooleanVar(value=False)
        self.backup_dir = tk.StringVar(value=os.path.expanduser("~"))

        pr.set_log_sink(self._log_sink)

        self._build_device_bar(root)
        self._build_actions(root)
        self._build_panes(root)
        self._build_statusbar(root)

        self.refresh_disks()
        if os.geteuid() != 0:
            self.say("not running as root — raw device access will fail; relaunch with sudo")
            messagebox.showwarning("partrevive",
                "Run as root (sudo) — partrevive reads raw block devices and writes "
                "partition tables.")
        root.protocol("WM_DELETE_WINDOW", root.destroy)

    # ---------------------------------------------------------------- layout
    def _build_device_bar(self, root):
        bar = ttk.LabelFrame(root, text="Target disk", padding=8)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        self.disk_cb = ttk.Combobox(bar, textvariable=self.dev, width=46, state="readonly")
        self.disk_cb.grid(row=0, column=0, padx=(0, 6))
        self.disk_cb.bind("<<ComboboxSelected>>", lambda e: self.on_pick_disk())
        ttk.Button(bar, text="⟳ Refresh", command=self.refresh_disks).grid(row=0, column=1)
        ttk.Checkbutton(bar, text="deep scan", variable=self.deep).grid(row=0, column=2, padx=10)
        self.disk_info = ttk.Label(bar, text="", foreground="#555")
        self.disk_info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_actions(self, root):
        act = ttk.Frame(root, padding=(6, 8))
        act.pack(fill="x")
        self.scan_btn = ttk.Button(act, text="① Scan & Verify  (read-only)", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.rescue_btn = ttk.Button(act, text="⤓ Rescue files…", command=self.do_rescue, state="disabled")
        self.rescue_btn.pack(side="left", padx=(6, 0))
        self.plan_btn = ttk.Button(act, text="② Build Plan", command=self.build_plan, state="disabled")
        self.plan_btn.pack(side="left", padx=6)
        self.restore_btn = ttk.Button(act, text="③ Restore  (write table)", command=self.do_restore, state="disabled")
        self.restore_btn.pack(side="left")
        ttk.Checkbutton(act, text="MBR", variable=self.mbr).pack(side="left", padx=6)
        self.prog = ttk.Progressbar(act, mode="determinate", length=160)
        self.prog.pack(side="right")
        self.image_btn = ttk.Button(act, text="🖫 Image (ddrescue)…", command=self.do_image)
        self.image_btn.pack(side="right", padx=8)

    def _build_panes(self, root):
        pan = ttk.PanedWindow(root, orient="vertical")
        pan.pack(fill="both", expand=True, padx=6, pady=4)

        cf = ttk.LabelFrame(pan, text="Candidates  (live = real data · ghost = overwritten remnant)", padding=4)
        cols = ("status", "fstype", "start", "size", "type", "info")
        self.ctree = ttk.Treeview(cf, columns=cols, show="headings", height=8)
        for c, w, a in (("status", 70, "center"), ("fstype", 60, "center"), ("start", 110, "e"),
                        ("size", 90, "e"), ("type", 60, "center"), ("info", 360, "w")):
            self.ctree.heading(c, text=c); self.ctree.column(c, width=w, anchor=a)
        for status, (tag, color) in TAGCOLOR.items():
            self.ctree.tag_configure(tag, foreground=color)
        self.ctree.pack(side="left", fill="both", expand=True)
        cs = ttk.Scrollbar(cf, orient="vertical", command=self.ctree.yview); cs.pack(side="right", fill="y")
        self.ctree.configure(yscrollcommand=cs.set)
        pan.add(cf, weight=2)

        pf = ttk.LabelFrame(pan, text="Proposed partition table", padding=4)
        pcols = ("n", "start", "end", "size", "code", "type")
        self.ptree = ttk.Treeview(pf, columns=pcols, show="headings", height=5)
        for c, w, a in (("n", 30, "center"), ("start", 110, "e"), ("end", 110, "e"),
                        ("size", 90, "e"), ("code", 60, "center"), ("type", 320, "w")):
            self.ptree.heading(c, text=c); self.ptree.column(c, width=w, anchor=a)
        self.ptree.pack(fill="both", expand=True)
        pan.add(pf, weight=1)

        lf = ttk.LabelFrame(pan, text="Log", padding=4)
        self.logbox = tk.Text(lf, height=7, bg="#101014", fg="#d8d8dc",
                              insertbackground="#d8d8dc", font=("Consolas", 9), wrap="word")
        self.logbox.pack(side="left", fill="both", expand=True)
        ls = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview); ls.pack(side="right", fill="y")
        self.logbox.configure(yscrollcommand=ls.set, state="disabled")
        pan.add(lf, weight=1)

    def _build_statusbar(self, root):
        self.status = tk.StringVar(value="ready")
        bar = ttk.Frame(root); bar.pack(fill="x", side="bottom")
        ttk.Separator(bar).pack(fill="x")
        row = ttk.Frame(bar); row.pack(fill="x")
        ttk.Label(row, textvariable=self.status, anchor="w", padding=4).pack(side="left", fill="x", expand=True)
        ttk.Label(row, text="backup dir:").pack(side="left")
        ttk.Entry(row, textvariable=self.backup_dir, width=22).pack(side="left", padx=2)
        ttk.Button(row, text="…", width=3, command=self.pick_backup_dir).pack(side="left", padx=(0, 4))

    # ---------------------------------------------------------------- helpers
    def say(self, m): self.status.set(m)

    def _log_sink(self, msg):
        clean = ANSI.sub("", msg)
        self.root.after(0, lambda: self._append_log(clean))

    def _append_log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n"); self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def pick_backup_dir(self):
        d = filedialog.askdirectory(initialdir=self.backup_dir.get())
        if d: self.backup_dir.set(d)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.scan_btn.config(state=state)
        self.image_btn.config(state=state)
        self.disk_cb.config(state="disabled" if busy else "readonly")
        if busy:
            for b in (self.plan_btn, self.restore_btn, self.rescue_btn):
                b.config(state="disabled")

    def refresh_disks(self):
        try:
            self.disks = pr.list_disks()
        except Exception as e:
            self.disks = []; self.say(f"lsblk failed: {e}")
        labels = [f"{d['dev']}   {d['size']:>7}  {d['tran'] or '—':<4} {d['model']}".rstrip()
                  for d in self.disks]
        self.disk_cb["values"] = labels
        if labels and not self.dev.get():
            self.disk_cb.current(0); self.on_pick_disk()
        self.say(f"{len(self.disks)} disk(s) found")

    def _cur_dev(self):
        i = self.disk_cb.current()
        return self.disks[i]["dev"] if 0 <= i < len(self.disks) else None

    def on_pick_disk(self):
        dev = self._cur_dev()
        if not dev: return
        d = self.disks[self.disk_cb.current()]
        warn = ""
        if d["tran"] == "usb":
            warn = "   · USB bridge: fine for recovery, but secure-erase/format may be filtered"
        self.disk_info.config(text=f"{dev} — {d['size']} {d['model']}{warn}")
        # reset state when target changes
        self.cands = []; self.parts = []
        self.ctree.delete(*self.ctree.get_children())
        self.ptree.delete(*self.ptree.get_children())
        for b in (self.plan_btn, self.restore_btn, self.rescue_btn):
            b.config(state="disabled")

    # ---------------------------------------------------------------- ① scan
    def start_scan(self):
        dev = self._cur_dev()
        if not dev:
            messagebox.showinfo("partrevive", "Pick a disk first."); return
        if pr._is_mounted(dev):
            if not messagebox.askyesno("partrevive",
                f"{dev} has a mounted partition. Scanning is read-only and safe, "
                "but you cannot Restore while it's mounted. Continue scanning?"):
                return
        self.cands = []; self.parts = []
        self.ctree.delete(*self.ctree.get_children())
        self.ptree.delete(*self.ptree.get_children())
        self.prog["value"] = 0
        self._set_busy(True)
        self.say(f"scanning {dev} … (reads the whole surface; large disks take a while)")
        threading.Thread(target=self._scan_worker, args=(dev, self.deep.get()), daemon=True).start()

    def _scan_worker(self, dev, deep):
        try:
            cands = pr.scan(dev, deep=deep, progress=False,
                            on_progress=lambda p: self.root.after(0, lambda: self.prog.configure(value=p)))
            if not cands:
                self.root.after(0, lambda: (self.say("no filesystem signatures found"), self._set_busy(False)))
                return
            self.root.after(0, lambda: self.say(f"verifying {len(cands)} candidate(s) read-only…"))
            pr.verify(dev, cands, on_result=lambda c: self.root.after(0, lambda c=c: self._add_cand_row(c)))
            self.cands = cands
            self.root.after(0, self._scan_done)
        except Exception as e:
            self.root.after(0, lambda: (self.say(f"scan error: {e}"), self._set_busy(False)))

    def _add_cand_row(self, c):
        tag, _ = TAGCOLOR.get(c.confidence, ("sig", ""))
        label = c.note or (c.blkid.replace('"', '') if c.blkid else c.fstype)
        size = "?" if c.report_only else human(c.size)
        self.ctree.insert("", "end", tags=(tag,), values=(
            c.confidence, c.fstype, f"{c.start:,}", size, c.typecode, label))

    def _scan_done(self):
        self._set_busy(False)
        live = sum(1 for c in self.cands if c.confidence == "mounted" and c.fstype != "swap")
        self.prog["value"] = 100
        self.say(f"scan complete — {len(self.cands)} candidate(s), {live} live")
        self.plan_btn.config(state="normal" if live else "disabled")
        self.rescue_btn.config(state="normal" if live else "disabled")

    # ---------------------------------------------------------------- rescue
    def do_rescue(self):
        dev = self._cur_dev()
        live = [c for c in self.cands if c.confidence == "mounted" and c.fstype != "swap"]
        if not dev or not live:
            return
        dest = filedialog.askdirectory(title="Copy recovered files into…",
                                       initialdir=self.backup_dir.get())
        if not dest:
            return
        self._set_busy(True)
        self.say(f"rescuing {len(live)} partition(s) → {dest} (read-only source)…")
        threading.Thread(target=self._rescue_worker, args=(dev, list(self.cands), dest),
                         daemon=True).start()

    def _rescue_worker(self, dev, cands, dest):
        try:
            res = pr.rescue(dev, cands, dest)
            n = sum(1 for _, ok, _ in res["partitions"] if ok)
            self.root.after(0, lambda: (self._set_busy(False),
                self.say(f"rescue done — {n}/{len(res['partitions'])} partition(s) → {dest}"),
                messagebox.showinfo("partrevive", f"Copied {n} partition(s) into:\n{dest}")))
        except Exception as e:
            self.root.after(0, lambda: (self._set_busy(False), self.say(f"rescue error: {e}")))

    # ---------------------------------------------------------------- ② plan
    def build_plan(self):
        dev = self._cur_dev()
        if not dev or not self.cands: return
        self.parts = pr.plan(self.cands, dev)
        self.ptree.delete(*self.ptree.get_children())
        for i, c in enumerate(self.parts, 1):
            self.ptree.insert("", "end", values=(
                i, f"{c.start:,}", f"{c.end:,}", human(c.size), c.typecode, c.note or c.fstype))
        mounted = pr._is_mounted(dev)
        ok = bool(self.parts) and not mounted
        self.restore_btn.config(state="normal" if ok else "disabled")
        self.say(f"plan: {len(self.parts)} partition(s)"
                 + ("  — unmount the disk to enable Restore" if mounted else "  — review, then Restore"))

    # ---------------------------------------------------------------- ③ restore
    def do_restore(self):
        dev = self._cur_dev()
        if not dev or not self.parts: return
        if pr._is_mounted(dev):
            messagebox.showerror("partrevive", f"{dev} is mounted — unmount it first."); return
        ok, smsg = pr.smart_health(dev)
        if ok is False:
            if not messagebox.askyesno("Drive health warning",
                    f"⚠ {smsg}\n\nThis drive reports failing health. Writing to it risks "
                    "further damage — imaging it with ddrescue first is safer.\n\n"
                    "Write the partition table anyway?", icon="warning", default="no"):
                self.say("restore cancelled — drive reports failing health"); return
        health = f"\nDrive health: {smsg}\n" if ok is not None else ""
        summary = "\n".join(
            f"  {i}.  {c.start:>12,} – {c.end:<12,}  {human(c.size):>9}  {c.typecode}  {c.note or c.fstype}"
            for i, c in enumerate(self.parts, 1))
        if not messagebox.askyesno("Write partition table?",
                f"Write this GPT to {dev}?\n\n{summary}\n{health}\n"
                f"The current table is backed up to:\n  {self.backup_dir.get()}\n\n"
                "This writes only the partition table (not your files), and is undoable "
                "from the backup. Proceed?"):
            return
        self._set_busy(True)
        self.say(f"writing partition table to {dev} …")
        threading.Thread(target=self._restore_worker, args=(dev, list(self.parts)), daemon=True).start()

    def _restore_worker(self, dev, parts):
        try:
            if self.mbr.get():
                backup = pr.write_mbr(dev, parts, self.backup_dir.get())
            else:
                backup = pr.write_gpt(dev, parts, self.backup_dir.get())
            self.root.after(0, lambda: self._restore_done(dev, backup))
        except Exception as e:
            self.root.after(0, lambda: (self.say(f"restore error: {e}"), self._set_busy(False)))

    # ---------------------------------------------------------------- image
    def do_image(self):
        dev = self._cur_dev()
        if not dev:
            messagebox.showinfo("partrevive", "Pick a disk first."); return
        if not __import__("shutil").which("ddrescue"):
            messagebox.showerror("partrevive",
                "ddrescue not installed.\n\nInstall it with:  sudo apt install gddrescue"); return
        out = filedialog.asksaveasfilename(title="Save disk image as…",
                                           initialdir=self.backup_dir.get(),
                                           defaultextension=".img",
                                           filetypes=[("Disk image", "*.img"), ("All", "*")])
        if not out:
            return
        self._set_busy(True)
        self.say(f"imaging {dev} → {out} (ddrescue, two passes; watch the Log/terminal)…")
        threading.Thread(target=self._image_worker, args=(dev, out), daemon=True).start()

    def _image_worker(self, dev, out):
        try:
            pr.image_drive(dev, out)
            self.root.after(0, lambda: (self._set_busy(False),
                self.say(f"image complete → {out}"),
                messagebox.showinfo("partrevive",
                    f"Imaged to:\n{out}\n\nNow recover from the copy: pick {out} as the "
                    "target (or run `partrevive auto {out}`).")))
        except Exception as e:
            self.root.after(0, lambda: (self._set_busy(False), self.say(f"image error: {e}")))

    def _restore_done(self, dev, backup):
        self._set_busy(False)
        self.restore_btn.config(state="disabled")
        self.say(f"done — table written to {dev}")
        messagebox.showinfo("partrevive",
            f"Partition table written to {dev}.\n\n"
            f"Backup of the previous table:\n  {backup}\n\n"
            f"Undo with:\n  sgdisk --load-backup={backup} {dev}\n\n"
            "Now mount the partitions read-only and confirm your data.")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except Exception:
        pass
    PartReviveGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
