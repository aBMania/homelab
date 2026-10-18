# VM and app state on the SSD, backed up to the HDD pool: design

Date: 2026-10-18. Status: Stage 1 done (2026-10-18); Stage 2 designed, waiting for review.

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
| Backups | daily snapshots of `tank/talos` + `tank/apps` → `tank2/backup` | **hourly** snapshots of the SSD disks → back to the HDD pool (into the old `tank/talos`, incrementally); `tank/apps` daily → `tank2/backup` **excluding `tank/apps/emby`** until Stage 2, then removed; per-app backups (Stage 3) |

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
     + daily (keep 14 d). The `tank/apps` → `tank2/backup/apps` task keeps running, but excludes
     `tank/apps/emby` (~20 GB of Emby `sync` conversions, re-creatable).
   - Rollback: point the VM back at `tank/talos` (as of the switch, or a later replicated
     snapshot).
2. **Separate data disk + local volumes** (drops Longhorn). Detailed in
   [Stage 2 in detail](#stage-2-in-detail) below.
3. **Per-app backups.**
   - CNPG → S3 (the MinIO in `clusterenv`), with Barman backups and WAL archiving: point-in-time
     restore per database.
   - VolSync (already installed) → restic to MinIO for the hostpath volumes: restore one app
     without rolling back a whole disk.

## Stage 2 in detail

Decided with the user on 2026-10-18 unless marked *(my call)*.

### Scope

| Today | Moves to the data disk |
|---|---|
| Longhorn, 17 volumes, 31 GB used | media configs: emby, radarr, sonarr, radarr-4k, prowlarr, bazarr, jellyseerr, pinchflat; cloudflareddns config; CNPG clusters authelia (1 instance), lldap (1), whosaid (2) |
| NFS `tank/apps` | qbittorrent config (160 MB), recyclarr config, authelia config, lldap data |
| Stays | media (NFS), Emby `sync` (NFS `tank/apps/emby/sync`), Emby transcode (NFS on `tank2`), jupyter data (NFS, a notebook workspace, not app state) *(my call)* |

### Downtime rule (user)

Downtime is fine **as long as nobody is playing anything in Emby**. Every step that stops an app or
the node first checks Emby's active sessions (`/Sessions`, any `NowPlayingItem`) and waits (or
stops) if one is found. No other time-of-day constraint.

The check uses a dedicated Emby API key named `maintenance` (created by the user in the Emby UI,
2026-10-18). It is stored SOPS-encrypted as Secret `emby-maintenance` (key `api-key`) in the
`media` namespace, in `media/emby/app/emby-maintenance.secret.yaml`; scripts and `node:down` read
it with `kubectl get secret`. Never create Emby keys through the API with another app's key (that
revoked Radarr's key on 2026-10-17).

### Steps

1. **Disk** (one maintenance window, the only node downtime):
   - On TrueNAS, a sparse zvol `tank2/talos-data`, 120 G, same properties as `tank2/talos`.
   - `mise run node:down shutdown` → add it as a second virtio disk on the `talos` VM → boot →
     `mise run node:up`.
   - Talos `UserVolumeConfig` named `data` (mounted at `/var/mnt/data`), selecting the disk by
     size or serial so it can never match the system disk; filesystem xfs (the Talos default).
     Committed as a Talos patch; applied with `mise run talos:diff` then `talos:apply` (user).
   - `installDiskSelector` (`size <= 1600GB`) would also match the 120 G disk, so the install
     selector and the UserVolumeConfig selector each exclude the other disk explicitly.
   - kubelet `extraMounts` for `/var/mnt/data` (`bind`, `rshared`, `rw`), like the existing
     `/var/openebs/local` one, so hostpath volumes are visible to the kubelet.
2. **StorageClass**: `local-data`, provisioner `openebs.io/local` with
   `BasePath: /var/mnt/data/openebs`, `WaitForFirstConsumer`, `reclaimPolicy: Retain`
   *(my call: a deleted PVC never deletes app data by accident)*. Made the **default** class; the
   `longhorn` default annotation is removed in the same change. The existing `openebs-hostpath`
   class (on the OS disk) is left alone.
3. **Backups of the data disk**: TrueNAS snapshot tasks on `tank2/talos-data` hourly (keep 48 h)
   and daily 03:00 (keep 14 d), replicated to `tank/talos-data` on the HDD pool (first run from
   scratch, then incremental), mirroring `ssd-to-hdd` for the OS disk. *(My call)* The two zvols
   stay separate datasets, so their snapshots are not atomic; both tasks use the **same schedule
   minute** so a restore pairs an OS snapshot and a data snapshot taken seconds apart. That is
   enough here: what etcd holds about storage (PVs, PVCs, CNPG cluster objects) changes rarely,
   and every app recovers from a crash-consistent copy.
4. **Configs** (one app per PR, smallest first: pinchflat, radarr-4k, prowlarr, bazarr,
   jellyseerr, cloudflareddns, recyclarr, qbittorrent, radarr, sonarr, emby; then authelia and
   lldap config files):
   - New PVC `<app>-data` on `local-data` (with `kustomize.toolkit.fluxcd.io/prune: disabled`),
     same size as today *(my call: a new name, because both claims must exist during the copy)*.
   - The same two-pass rsync method as the 2026-10-18 NFS → Longhorn move (scratchpad
     `cutover.sh`/`rsync-pass.sh`): pass 1 live, stop the app, pass 2, compare file counts,
     switch the HelmRelease to the new claim, then compare the app's API counts before/after.
   - The old Longhorn PVC / NFS folder is kept until Longhorn removal (rollback = revert the PR).
5. **Postgres (CNPG)** *(my call over the spec's `pg_basebackup`/dump options)*: change the
   cluster's storage class to `local-data` and add one instance → CNPG builds the new replica on
   the data disk → wait until it's streaming and in sync → switchover to it → back to the old
   instance count, so CNPG drops the old instance and its Longhorn PVCs (data + WAL). **whosaid
   goes from 2 instances to 1** (user, 2026-10-18: a second copy on the same node and disk
   protects against nothing): add one instance on `local-data`, switch over to it, then set
   `instances: 1`. Order:
   whosaid, lldap, authelia (auth last: a failure there blocks every login). All three charts are
   TrueCharts-`common` based (whosaid-rs too: its cluster is `whosaid-whosaid-rs-cnpg-main`), so
   instances and storage class are set through the HelmRelease's `cnpg.main` values; the
   clusters set no storage class today and so follow the default class. If a chart can't
   express the change, fall back to a new cluster bootstrapped with `pg_basebackup` from the
   old one.
6. **`node:down` / `node:up`**: discover apps by **any PVC** (not just `longhorn*` classes),
   still hibernate CNPG and scale workloads to 0; replace the "wait for Longhorn volumes to
   detach" step with "wait until those pods are gone". `--dry-run` stays.
7. **Remove Longhorn**, once `kubectl get pvc -A` shows no `longhorn*` claim:
   - set Longhorn's `deleting-confirmation-flag` (its uninstall job refuses otherwise), then
     remove `system/longhorn` from `system/kustomization.yaml`, the HelmRepository, the
     VolumeSnapshotClass and the `longhorn-static` class;
   - Talos: drop the `/var/lib/longhorn` extraMount and the `longhorn-system` namespace
     exemption in `patches/controlplane.yaml` (user applies);
   - AGENTS.md, `mise.toml`, scripts: remove the Longhorn notes;
   - delete the old NFS config folders and the `tank/apps` backup task + its replication (only
     Emby's re-creatable `sync` and leftovers stay there).
   - The OS disk keeps its size; the space Longhorn used is freed inside it (TRIM to reclaim it
     on the zvol is optional).

### Rollback

- Per app: revert its PR; the old PVC or NFS folder is untouched until step 7.
- Postgres: until the old instance is dropped, switch back; after, restore from the
  `tank2/talos` snapshot that still holds the Longhorn copy (the OS disk) or the data disk
  snapshots.
- The data disk itself: its zvol snapshots on `tank2` and their copy on `tank`.

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
- An off-site copy of the backups (user decision, 2026-10-18).
- Moving media off NFS.
- The quick wins already in progress: Pinchflat `TMPFILE_PATH` (#1004) and throughput,
  scrub throttling.
