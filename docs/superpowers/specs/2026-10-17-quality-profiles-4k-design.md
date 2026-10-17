# Quality profiles (French MULTi.VO) + 4K twin library: design

Date: 2026-10-17. Status: approved 2026-10-17.

## Goal

- Releases must contain the **original audio (VO)**. French audio is preferred but optional:
  a MULTi release (VO + VF) beats a VO-only one, and a French dub without the VO is rejected.
- **1080p by default** for movies and series.
- A **few selected movies also get a 4K version**, kept *next to* the 1080p one: the 4K copy
  is for the projector (Anker Nebula X1: HDR10, HLG, Dolby Vision), and the 1080p copy serves
  every other screen without 4K transcoding on the GTX 960.
- Fix recyclarr, which has failed every night since its v8 upgrade (`quality_profiles` under
  `custom_formats` was renamed `assign_scores_to`). The cron wrapper still logs
  `job succeeded`, so nothing warned about it.

Out of scope: anime profiles, Jellyseerr override rules, Bazarr for the 4K library
(Bazarr supports a single Radarr).

## Current state (2026-10-17, aggregates only)

| | Radarr | Sonarr |
|---|---|---|
| Items / files | 264 movies, 209 files, 4.38 TB | 127 series, 3245 episode files, 5.34 TB |
| Profiles in use | built-ins only: `Any` 151, `HD-1080p` 59, `HD - 720p/1080p` 50, other 4 | `Any` 72, `HD-1080p` 37, `HD - 720p/1080p` 16, `HD-720p` 2 |
| recyclarr profiles in use | 0 (`HD Bluray + WEB` exists but is unassigned) | 0 (`WEB-1080p` unassigned) |
| Resolution | 2160p: 61, 1080p: 136, 720p: 4, 480p: 1, unknown: 7 | 1080p: 2326, 720p: 560, 480p: 353, 2160p: 6 |
| Audio vs original language | VO 148, French original 31, MULTi 28, **no VO 2** | VO 3063, French original 85, MULTi 41, **no VO 56** |
| Custom-format score of files | 0 everywhere | n/a |
| Recycle bin | disabled | disabled |

- Pool: `tank/media` uses 9.03 TB and has 5.06 TB free. There are no ZFS snapshots or backups
  of any kind, so a file deleted by an upgrade is gone for good.
- The recyclarr config lives on NFS (`/mnt/tank/apps/recyclarr/recyclarr.yml`), outside git.

## Target design

### Profiles (TRaSH Guides, synced by recyclarr)

| Instance | Profile (TRaSH name) | trash_id | Used for |
|---|---|---|---|
| radarr | `[French MULTi.VO] HD Bluray + WEB` | `2572ce3ea4eef1c19d59e0e20ed1cea7` | every movie |
| radarr-4k (new) | `[French MULTi.VO] UHD Bluray + WEB` | `92ead7022d13a7858d54e328e6a2f8f9` | selected movies |
| sonarr | `[French MULTi.VO] HD Bluray + WEB (1080p)` | `4c48f506c1116a3a57ae33f12346bd15` | every series |

Language behavior, verified in the TRaSH JSON:

| Release audio | Custom format | Score |
|---|---|---|
| VO + French (MULTi) | `Language: Original + French` | +500 |
| VO only | `Language: Not French` | 0 |
| French dub only / other dub | `Language: Not Original` | -10000 (rejected) |

The Radarr profiles also set `language: Original`. Sonarr profiles have no language field
and rely on the -10000 score.

HDR on radarr-4k: the default-synced `[HDR Formats] HDR` group scores DV/HDR10/HLG. The opt-in
group `[HDR Formats] DV (w/o HDR fallback)` (`7fc2751eef7e6bdc70b74136e5e35c76`) is
**enabled**, which rejects DV profile-5 releases with no HDR10 base layer. Their playback through
the Emby Android TV app is uncertain. This is a one-line change to revert after a successful test.

### recyclarr config in git

- The `recyclarr.yml` content moves to a ConfigMap in `media/recyclarr/app/`, mounted
  read-only in `/config/configs/` (recyclarr loads every `*.yml` there). The NFS
  `recyclarr.yml` is renamed `recyclarr.yml.bak` so it isn't loaded twice.
- API keys use `!env_var` (supported in recyclarr 8.7.3):
  - radarr and sonarr reuse `RADARR_API_KEY` and `SONARR_API_KEY` from the existing
    `decluttarr-secret` (namespace `media`) via `envFrom`.
  - radarr-4k gets a new `RADARR_4K_API_KEY` in a SOPS-encrypted `*.secret.yaml`.
- `base_url`s are in-cluster services (`http://radarr.media.svc.cluster.local:7878`, …).
- `delete_old_custom_formats: true` removes the stale CFs from the 2023 config.
- The v8 syntax comes from the current `french-multi-vo-*` templates (CF groups,
  `reset_unmatched_scores`).

### radarr-4k (new app)

- `media/radarr-4k`: a TrueCharts `radarr` release, same values as `media/radarr`, except:
  - config on NFS `/mnt/tank/apps/radarr-4k`
  - host `movies-4k.${DOMAIN_0}`, behind Authelia (admin group only: no family rule)
  - same `/media` mount, root folder `/media/movies-4k`
- TrueNAS prerequisites (run by the user, commands given in the plan):
  - dataset `tank/apps/radarr-4k`, plus an NFS share cloned from the `radarr` share with
    `midclt` (host = node IP)
  - `mkdir /mnt/tank/media/movies-4k`, owned by 568:568

### Integrations

| App | Change |
|---|---|
| Prowlarr | add radarr-4k as an application, so indexers are synced |
| qBittorrent | radarr-4k uses its own category `radarr-4k` |
| decluttarr | add radarr-4k to `RADARR:` (key in `decluttarr-secret`) |
| Jellyseerr | radarr-4k as the **4K server** (UHD profile, `/media/movies-4k`). "Request 4K" permission for admins only. Default servers switched to the new HD profiles |
| Emby | new **Movies 4K** library on `/media/movies-4k`; `Movies` stays on `/media/movies` |

## Migration of existing files

### Risks this has to handle

1. **Upgrade wave.** Every existing file scores 0 against a profile whose cutoff is 10000,
   so every switched item is "cutoff unmet". Upgrades driven by RSS stay gradual: only new
   releases that show up in the feeds trigger them. But **"Search All / Search Cutoff Unmet"**
   would queue roughly 470 movies and series at once. That button must not be used during the
   migration.
2. **Permanent deletion.** An upgrade deletes the old file; there's no recycle bin and no
   snapshot.
3. **No-VO files** (2 movies, 56 episodes) score -10000 and become replacement candidates. That
   matches the VO requirement, but language detection on old imports can be wrong. The user
   reviews that list in the Sonarr/Radarr UI before the bulk switch. Titles are never printed by
   tooling.
4. **61 movies are already 2160p** in the 1080p library. Radarr doesn't downgrade, so under the
   HD profile they would stay 4K forever, and remote viewers would keep forcing 4K transcodes.
5. **Emby watched state** may reset for movies whose path changes. This gets checked on the
   first batch.

### Phases

Each phase is a separate PR or step. The next one starts only after the previous one is
verified.

**P0: Safety net**
- `sudo zfs snapshot tank/media@pre-quality-migration` (instant; it holds replaced or deleted
  data until destroyed). Kept 30 days after P5, then destroyed. Its size grows with every file
  replaced during P3 and P5; check it weekly with `zfs list -t snapshot` and destroy it early if
  free space drops below 2 TB.
- Baseline counts are recorded with the aggregate script (counts only).

**P1: recyclarr fixed, new profiles created, nothing assigned**
- Config moved to git as above, for radarr and sonarr only.
- `recyclarr sync --preview` output is reviewed first, then the real sync runs.
- No item uses the new profiles yet, so download behavior doesn't change. The unused 2023
  profiles (`HD Bluray + WEB`, `WEB-1080p`) are deleted.

**P2: Canary**
- 5 movies and 2 series switched by hand to the new profiles, mixing VO, MULTi, French-original
  and 720p files.
- Immediately: check each file's CF score, and run an **Interactive Search** on each canary
  item. It shows every candidate's score and whether Radarr/Sonarr would grab it, with nothing
  downloaded.
- Then 24 hours of Activity → Queue and decluttarr logs, for unexpected automatic (RSS) grabs.

**P3: Bulk switch**
- After the user reviews the no-VO list: Radarr and Sonarr Mass Editor → every item onto the
  new HD profile. Unmonitored items too, since that's harmless.
- Jellyseerr default profiles get switched as well.
- Watched for 2 weeks. **Rollback** = Mass Editor back to the old built-in profile, which stays
  available until P6. Abort threshold: more than 30 grabs per day sustained over 3 days.

**P4: radarr-4k**
- TrueNAS prerequisites, the app PR, then integrations (Prowlarr, qBittorrent category,
  decluttarr, recyclarr UHD instance, Jellyseerr 4K server, Emby Movies 4K library).
- Verified with one new 4K request from Jellyseerr, end to end.

**P5: Move the 61 existing 4K movies into the 4K library**
- Script `scripts/migrate-4k.py` (mise task, `--dry-run` by default, prints counts only),
  working in batches of 10:
  1. In radarr, set the movie's root folder to `/media/movies-4k` with `moveFiles=true`.
     Radarr moves the folder, extras and subtitles included. It's a same-dataset rename,
     so it's instant.
  2. Remove the movie from radarr, keeping its files.
  3. In radarr-4k, add it (TMDB id, UHD profile, path `/media/movies-4k/...`) and rescan,
     so the existing file is picked up with no download.
  4. Re-add it in radarr (HD profile, `/media/movies`) **without search**.
- A 1080p twin is then searched at most 10 per day. That adds about 0.6 TB in total
  (61 × ~10 GB), well within the 5.06 TB free.
- Afterwards, the user can delete the 4K versions of movies that don't deserve one, from
  radarr-4k (delete files). That frees about 30 GB each.
- The 6 2160p episodes stay as they are: Sonarr doesn't downgrade, and that's harmless.

**P6: Cleanup (after 30 days)**
- Delete the unused built-in profiles in both apps.
- `zfs destroy tank/media@pre-quality-migration`.
- Update `AGENTS.md` (profile layout, radarr-4k) and memory.

## Validation

- Every PR: `kustomize build` + `scripts/kubeconform.sh`.
- recyclarr: `--preview` before the first real sync, then the nightly log must show no `[ERR]`.
- Per phase: queue size and grab counts from the aggregate script. Titles never get printed.

## Decisions

- **P5: keep all 61 existing 4K files** (approved) as 4K versions and add 1080p twins. That's
  the no-loss path, and the user prunes 4K versions afterwards. The alternative is to pick the
  movies that stay 4K first and convert the rest to 1080p only. That saves about 1.8 TB, but
  deletes files.
- DV without HDR10 fallback: rejected until it's tested on the projector.
