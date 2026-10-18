# VM and app state on the SSD, backed up to the HDD pool: design

Date: 2026-10-18. Status: draft, waiting for review.

## Why

Three outages on 2026-10-17/18 had the same cause: heavy I/O on the HDD raidz2 pool `tank` starved
**etcd**, which lives on the VM disk (`tank/talos`) on that same pool, and the Kubernetes API went
down (Emby's VP9 crash loop, the weekly scrub with the node NotReady 03:00–03:19 UTC, and
Pinchflat's unthrottled downloads into `/tmp`). Media, NFS configs and the VM disk all share 6
HDDs, so any big job on the media takes the control plane down.

## Decisions (user, 2026-10-18)

- **One SSD, no mirror** (no second SSD planned). Its protection is a **copy to the HDD pool**.
- Target layout as below; Longhorn → local volumes in a later stage.

## Current → target

| | Now | Target |
|---|---|---|
| Talos OS + etcd + container images | VM disk `tank/talos` (HDD) | **OS disk on SSD** (`tank2/talos`) |
| App state (configs, SQLite, Postgres) | Longhorn (1 replica) inside the VM disk (HDD) | **separate SSD disk** (`tank2/talos-data`), `openebs-hostpath` local volumes (Stage 2) |
| Media | NFS `tank/media` (HDD) | unchanged |
| Emby transcode | NFS `tank2/apps/emby-transcode` (SSD) | unchanged |
| Backups | daily snapshots of `tank/talos` + `tank/apps` → `tank2/backup` | **hourly** snapshots of the SSD disks → back to the HDD pool (into the old `tank/talos`, incrementally); `tank/apps` daily → `tank2/backup` as today; per-app backups (Stage 3) |

Sizing: Longhorn data 31 G used (995 G provisioned), container images 24 G, VM disk 111 G on ZFS
(246 G logical, 2.4× compression). SSD: 460 G, 343 G free today; the 114 G VM-disk copy already on it becomes the live disk. Planned: OS disk 120 G and data disk 120 G (thin zvols); together with
transcode and the `tank/apps` copy that's well under 460 G.

## Risk of a single SSD, and the RPO

If the SSD dies, the VM and its data are restored from the last replicated snapshot on `tank`.
**Data loss = time since the last replication.** Hourly snapshots + replication (incrementals:
small, minutes of SSD → HDD writes) give **RPO ≤ 1 h**. Restore = `zfs send` back onto a new SSD
(or temporarily onto `tank`, slower but running) and boot. Snapshots of a running VM are
crash-consistent, which etcd, SQLite and Postgres recover from.

Prerequisite: check the SSD's SMART health and wear before it carries the live system.

## Stages

1. **Move the VM disk to the SSD, as is** (keeps Longhorn). Most of the stability gain on its own.
   - **The copy already exists:** `tank2/backup/talos` is the nightly replica of `tank/talos`
     (Task 0 of the app-config storage plan). No bulk copy needed.
   - `node:down shutdown` → snapshot `tank/talos` and run the replication one last time
     (incremental, only today's changes) → disable that replication task → on
     `tank2/backup/talos`: `readonly=off`, rename to `tank2/talos` → point the VM's disk at the
     `tank2/talos` zvol → boot → `node:up`. Downtime ≈ the final incremental + the reboot.
   - **Reverse the backup:** the old `tank/talos` shares the same snapshots, so a new task
     replicates `tank2/talos` → `tank/talos` **incrementally** (no full copy). The old disk is then
     both the rollback disk and the backup target. Snapshots of `tank2/talos`: hourly (keep 48 h)
     + daily (keep 14 d). The `tank/apps` → `tank2/backup/apps` task stays as it is.
   - Rollback: point the VM back at `tank/talos` (as of the switch, or a later replicated
     snapshot).
2. **Separate data disk + local volumes** (drops Longhorn).
   - New zvol `tank2/talos-data` → second VM disk → Talos `UserVolumeConfig` mounted at e.g.
     `/var/mnt/data` → `openebs-hostpath` StorageClass rooted there, as the new default.
   - Apps move one by one with the two-pass rsync method (as on 2026-10-18); CNPG clusters via
     a new cluster bootstrapped from the old one (`pg_basebackup`) or a dump/restore.
   - `node:down`/`node:up` updated: they discover apps by Longhorn volumes today.
   - Then Longhorn is uninstalled. The OS disk shrinks to OS + images.
3. **Per-app backups and off-site.**
   - CNPG → S3 (the MinIO in `clusterenv`), with Barman backups and WAL archiving: point-in-time
     restore per database.
   - VolSync (already installed) → restic to MinIO for the hostpath volumes: restore one app
     without rolling back a whole disk.
   - Off-site copy of `tank/backup` (USB disk or cloud), later.

## What it improves

- **Stability:** etcd and app databases no longer share disks with media scans, scrubs and
  downloads, so the outage pattern above stops.
- **Speed:** small random I/O (SQLite, etcd) goes from a few hundred IOPS (raidz2 HDD) to tens of
  thousands (SSD).
- **Less overhead** (Stage 2): no Longhorn engine/replica processes or upgrades.
- **Backups that match the risk:** the live system on a single SSD, copied hourly to redundant
  HDDs; per-app restores in Stage 3.

## Out of scope

- A second SSD or mirror (user decision).
- Moving media off NFS.
- The quick wins already in progress: Pinchflat `TMPFILE_PATH` (#1004) and throughput,
  scrub throttling.
