# App configs off NFS: implementation plan

Date: 2026-10-17. Status: Task 0 decided (2026-10-17); Tasks 1–5 waiting for review.

## Why

The Servarr apps keep their SQLite databases in WAL mode on NFS (`/mnt/tank/apps/<app>`).
SQLite documents that WAL mode doesn't work on network filesystems. In practice the apps
freeze for more than 75 s and get killed by their liveness probe:

| App | Liveness failures / restarts (2026-10-08 → 17) |
|---|---|
| radarr | 899 / 17 (also went down twice during the 4K migration) |
| sonarr | 919 / 26 |
| prowlarr | 36 |
| radarr-4k on NFS | 14 restarts in ~3 h → **0 after moving to Longhorn** (#988) |

Emby's databases have the same setup. It shows no freezes, but the risk there is
corruption of watched states, users and the library.

## Target

| Data | Storage | Reason |
|---|---|---|
| App config and databases (radarr, sonarr, prowlarr, emby) | **Longhorn** PVC on the VM disk (zvol `tank/talos`, HDD raidz2, 500 G with ~105 G used) | local block storage, so SQLite locking works |
| Media library `/mnt/tank/media` (~9 TB) | NFS (unchanged) | shared by several apps (hardlinks), visible to TrueNAS, too big for the VM |
| Emby `sync` (20 GB of converted copies) | NFS (unchanged), its own mount at `/config/sync` | big plain files; keeps the PVC small |
| Emby `transcode` | NFS on `tank2` SSD (unchanged) | fast scratch space; tank2 is a single disk without redundancy, so nothing durable goes there |
| bazarr, jellyseerr, qbittorrent | NFS (unchanged) | no problems seen (bazarr doesn't use WAL) |

Sizes: radarr 508 M → 2 Gi PVC, sonarr 236 M → 2 Gi, prowlarr 19 M → 1 Gi, emby without
`sync` ~2 G → 10 Gi. Longhorn runs 1 replica (single node). `node:down`/`node:up` already
auto-discover Longhorn mounters.

## Global constraints

- One app per PR, one app per day. Each cutover is verified before the next one starts.
- Every change under `clusters/` deploys on merge. The user merges. Commands touching the live
  cluster (`flux suspend/resume`, `kubectl scale`, the copy pod) are run by the user, or by Claude
  when the user explicitly asks.
- **The NFS data is copied, never moved or deleted**, until the 2-week rollback window is over.
- PVCs use `retain: true` / `helm.sh/resource-policy: keep`, as for radarr-4k.
- No full Emby library refresh (see memory: it took the cluster down on 2026-10-17).

## Task 0: backups first (decided 2026-10-17)

Today nothing is backed up: TrueNAS has no snapshot or replication tasks, and Longhorn has no
backup target. Moving configs into the VM disk makes that disk the only copy. So, before Task 1,
the user creates these in TrueNAS (Data Protection; they need root):

**Periodic snapshot tasks:**

| Dataset | Recursive | Schedule | Keep | Covers |
|---|---|---|---|---|
| `tank/talos` (VM zvol, thin: 105 G used) | no | daily 03:00 | 14 days | etcd + every Longhorn volume (radarr-4k, pinchflat, the CNPG DBs, and the app configs after Tasks 1–4) |
| `tank/apps` (24 G) | **yes, jellyseerr included** (user, 2026-10-17) | daily 03:15 | 14 days | the configs still on NFS (emby `sync`, bazarr, qbittorrent, jellyseerr, …) |

No task for `tank/media`: upgrades replace files, so snapshots there would keep growing
(the one-off `@pre-quality-migration` stays until the plan's cleanup).

**Replication task (local, to the SSD pool):**
- Sources `tank/talos` and `tank/apps` (recursive) → destination **`tank2/backup`**, using the
  two snapshot tasks above; retention "same as source"; run after each snapshot.
- Size: about 130 G against 444 G free on tank2.
- What it protects against: losing the HDD pool, or a controller fault. Not the physical cases
  (fire, theft, PSU): tank2 is in the same box and is a single disk. An off-box copy (USB disk or
  a cloud bucket) of these ~130 G comes later.

**Verify (Claude, read-only):** `midclt call pool.snapshottask.query` and `replication.query`
list the tasks. After the first night: `zfs list -t snapshot` shows one per dataset, and
`zfs list -r tank2/backup` exists and has roughly the source sizes.

Snapshots of a running VM's disk are crash-consistent, like a power cut at that instant. etcd,
SQLite and Postgres recover from that.

## Per-app cutover (Tasks 1–3: radarr, sonarr, prowlarr)

**PR (one per app):**
- Add `app/<app>-config-pvc.yaml`: a PVC `<app>-config`, `storageClassName: longhorn`,
  `ReadWriteOnce`, size as above, annotated `helm.sh/resource-policy: keep`. List it in the
  app's `kustomization.yaml`.
- HelmRelease: `persistence.config` → `type: pvc`, `existingClaim: <app>-config`. Check with
  `helm template` that the Deployment mounts claim `<app>-config` at `/config`, and that
  `mise run validate` passes.

**Cutover (user-run, about 5 min of downtime):**
1. `flux suspend hr -n media <app>`, then `kubectl -n media scale deploy/<app> --replicas=0`,
   and wait until the pod is gone. The database is closed now, so the `-wal`/`-shm` files are
   consistent with it.
2. Merge the PR. The Kustomization creates the PVC; the suspended HelmRelease doesn't change yet.
3. Copy: a one-off pod (`busybox`, runAsUser/runAsGroup/fsGroup 568) mounting the NFS path
   read-only at `/src` and the PVC at `/dst`, running `cp -a /src/. /dst/ && ls -la /dst`.
   Then delete the pod.
4. `flux resume hr -n media <app>`. Helm applies the new values, and the pod starts on the PVC.

**Verify:**
- `mise run media:stats`: item and file counts are unchanged. API key, download client and
  indexers still work (the connection tests pass).
- 24 h later: 0 restarts, no liveness failures in the events.

**Rollback:** revert the PR. The app goes back to its untouched NFS data, losing only changes
made since the cutover.

## Task 4: emby

Same cutover, plus:
- New persistence `sync`: NFS `/mnt/tank/apps/emby/sync`, mounted at `/config/sync`. The PVC
  holds the rest of `/config`.
- The copy excludes `sync/` (`cd /src && tar --exclude=./sync -cf - . | tar -xf - -C /dst`),
  about 2 GB.
- Verify: users, watched states (compare per-TMDB played counts with a baseline taken just before
  the cutover, as done for the 4K migration), API keys (Jellyseerr's Emby test, radarr/radarr-4k
  Emby connection tests).

## Task 5: cleanup (2 weeks after the last cutover)

- Delete the old NFS datasets and shares: `tank/apps/{radarr,sonarr,prowlarr}`,
  `tank/apps/emby` except `sync`, and the unused `tank/apps/radarr-4k` (since #988).
- Update `AGENTS.md`: app configs live on Longhorn PVCs, media on NFS, plus the backup choice.

## Out of scope

- NetworkPolicy for `media` (follow-up from the qBittorrent and External-auth discussion).
- Putting Longhorn on the SSD (needs backups first; tank2 has no redundancy).
