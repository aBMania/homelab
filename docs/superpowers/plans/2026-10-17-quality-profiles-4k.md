# Quality profiles (French MULTi.VO) + 4K twin library: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every movie and series on a TRaSH French MULTi.VO 1080p profile, a new radarr-4k
instance holding 4K twins, and the 61 existing 4K files moved there with no re-download.

**Architecture:**
- recyclarr config moves into git as a ConfigMap; API keys come from SOPS secrets via
  `!env_var`.
- radarr-4k is a second TrueCharts radarr release, wired to qBittorrent, Prowlarr, decluttarr,
  Jellyseerr and Emby.
- A stdlib-only Python script migrates the existing 4K files through the Radarr API, in
  resumable batches.

**Tech Stack:** Flux/kustomize, TrueCharts charts (radarr 26.x / recyclarr 15.12.1),
recyclarr 8.7.3, Radarr 6.4 API v3, Sonarr v4 API v3, Python 3.11 stdlib (`unittest`,
`urllib`), SOPS/age, mise tasks.

**Spec:** `docs/superpowers/specs/2026-10-17-quality-profiles-4k-design.md`

## Global Constraints

- The repo is public: no domain (use `${DOMAIN_0}`), no IPs, **no media titles** in commits,
  PRs, files or script output.
- Every change under `clusters/` deploys on merge. Each task = one PR; the user merges. Never add
  the `automerge` label. Commits have no Co-Authored-By trailer.
- Secrets live only in `*.secret.yaml` files encrypted with `sops -e -i`; the pre-commit
  `sops:check` must pass.
- The user runs anything that needs `sudo` on TrueNAS or `kubectl exec`/`apply`; the plan gives
  the exact command.
- Profiles (trash_id): radarr `2572ce3ea4eef1c19d59e0e20ed1cea7` = `[French MULTi.VO] HD Bluray + WEB`;
  radarr-4k `92ead7022d13a7858d54e328e6a2f8f9` = `[French MULTi.VO] UHD Bluray + WEB`;
  sonarr `4c48f506c1116a3a57ae33f12346bd15` = `[French MULTi.VO] HD Bluray + WEB (1080p)`.
- radarr-4k also enables CF group `7fc2751eef7e6bdc70b74136e5e35c76` (DV w/o HDR fallback).
- Root folders: `/media/movies` (radarr), `/media/movies-4k` (radarr-4k). Both apps mount
  `/mnt/tank/media` at `/media`.
- "Search All" / "Search Cutoff Unmet" in Radarr/Sonarr must never be pressed during the
  migration.
- Validation on every manifest change: `mise run validate` (kustomize + kubeconform) exits 0.

## Review Focus

- **Interrupted migration.** The script is killed between two API calls. A rerun must resume,
  and a movie must never end up missing from both instances. Test:
  `test_resume_after_partial_move` (Task 6).
- **Folder already present in `/media/movies-4k`** (a manual copy, or an earlier run). Skip
  and count it; never overwrite. Test: `test_skips_folder_collision` (Task 6).
- **Unmonitored 4K movie.** It keeps its monitored state in both instances. Test:
  `test_preserves_monitored_flag` (Task 6).
- **Old NFS `recyclarr.yml` still loaded** next to the ConfigMap would sync twice or abort.
  It must be renamed *before* the Task 2 merge. That's a verification step in Task 2: the log
  lists only the new instances.
- **A title leaking into output or logs.** Test: `test_output_has_no_titles` (Tasks 1 and 6).

---

### Task 1: Safety snapshot + `media:stats` aggregate script

**Files:**
- Create: `scripts/media_stats.py`, `scripts/tests/test_media_stats.py`, `scripts/arr.sh`
- Modify: `mise.toml` (add task `media:stats`)

**Interfaces:**
- Produces `scripts/arr.sh`, sourced by the mise tasks:
  - it port-forwards `svc/radarr` → `localhost:17878`, `svc/sonarr` → `18989`, and
    `svc/radarr-4k` → `17879` (only if that svc exists);
  - it exports `RADARR_API_KEY`, `SONARR_API_KEY` and `RADARR_4K_API_KEY` (if its secret
    exists) from the cluster secrets;
  - an EXIT trap kills the forwards.
- Produces in `media_stats.py`:
  - `class Arr(base_url: str, api_key: str)` with `get(path) -> Any`,
    `post(path, body) -> Any`, `put(path, body) -> Any`, `delete(path) -> None`
    (`X-Api-Key` header, JSON bodies);
  - `audio_class(langs: list[str], original: str | None) -> str`, which returns one of
    `"unknown" | "multi" | "vo" | "french-original" | "no-vo"`;
  - `summarize_movies(movies: list[dict], profiles: dict[int,str]) -> dict` and
    `summarize_episodes(series: list[dict], files_by_series: dict[int,list[dict]], profiles) -> dict`;
  - `grabs_last_24h(history: list[dict], now: datetime) -> int`.
  Task 6 reuses `Arr` and `audio_class`.

- [ ] **Step 1: User takes the snapshot on TrueNAS.** Give them:
  `sudo zfs snapshot tank/media@pre-quality-migration && zfs list -t snapshot -o name,used | grep pre-quality`
  Expected: one line with `used` close to 0.
- [ ] **Step 2: Write the failing tests** in `scripts/tests/test_media_stats.py`:
  - `test_audio_class`: `(["English","French"],"English")→"multi"`, `(["English"],"English")→"vo"`,
    `(["French"],"French")→"french-original"`, `(["French"],"English")→"no-vo"`,
    `([],"English")→"unknown"`, `(["Unknown"],"English")→"unknown"`.
  - `test_summarize_movies`: on 3 fixture movies (one 2160p VO file, one 1080p no-VO, one
    without a file), it returns `with_file == 2`, `resolution == {2160: 1, 1080: 1}`,
    `audio == {"vo": 1, "no-vo": 1}`, and `profiles` counts by name.
  - `test_grabs_last_24h`: 3 `grabbed` events (one of them 25 h old) plus one
    `downloadFolderImported` → 2.
  - `test_output_has_no_titles`: run `main()` against a fake `Arr` whose fixtures have titles
    `"SECRET-TITLE-1"`…; the captured stdout doesn't contain `"SECRET-TITLE"`.
- [ ] **Step 3: Run** `python3 -m unittest discover -s scripts/tests -v`. Expected: FAIL
  (`ModuleNotFoundError: media_stats`).
- [ ] **Step 4: Implement** `media_stats.py` (stdlib only).
  - `main()` prints one JSON object: `radarr`, `sonarr`, and `radarr_4k` (when its key is set),
    each with the summary, `queue` (`/api/v3/queue` `totalRecords`) and `grabs_24h`
    (`/api/v3/history?eventType=1&pageSize=200`).
  - Episode files come from `/api/v3/episodefile?seriesId=`.
  - mise task: `run = "bash -c 'source scripts/arr.sh && python3 -I scripts/media_stats.py'"`.
- [ ] **Step 5: Run the tests.** Expected: PASS. Then run `mise run media:stats`. Expected: the
  radarr numbers match the spec's "Current state" (264 movies, 209 files, 61 at 2160p).
  Save the output as the baseline in the PR description.
- [ ] **Step 6: Commit** and open the PR "chore(media): media:stats aggregate script".

### Task 2: recyclarr config in git (radarr + sonarr)

**Files:**
- Create: `clusters/main/kubernetes/media/recyclarr/app/recyclarr-configs.yaml` (ConfigMap
  `recyclarr-configs`, key `french-multi-vo.yml`)
- Modify: `clusters/main/kubernetes/media/recyclarr/app/helm-release.yaml`,
  `clusters/main/kubernetes/media/recyclarr/app/kustomization.yaml`

**Interfaces:**
- Produces the ConfigMap `recyclarr-configs`. Task 5 adds the `radarr-4k` instance to the
  same file.
- Consumes the secret `decluttarr-secret` (keys `RADARR_API_KEY`, `SONARR_API_KEY`).

- [ ] **Step 1: Write `french-multi-vo.yml`** from the current upstream templates
  `radarr/templates/french-multi-vo-hd-bluray-web.yml` and
  `sonarr/templates/french-multi-vo-bluray-web-1080p.yml` (recyclarr/config-templates, master),
  keeping their default CF groups. Instances:
  - `radarr: radarr-hd` with `base_url: http://radarr.media.svc.cluster.local:7878` and
    `api_key: !env_var RADARR_API_KEY`;
  - `sonarr: sonarr-hd` with `base_url: http://sonarr.media.svc.cluster.local:8989` and
    `api_key: !env_var SONARR_API_KEY`.
  Each instance sets `delete_old_custom_formats: true` and `replace_existing_custom_formats: true`,
  plus the template's `quality_definition` and `quality_profiles` (trash_ids from the Global
  Constraints, with `reset_unmatched_scores.enabled: true`).
- [ ] **Step 2: HelmRelease values.**
  - Add `persistence.recyclarr-configs` with `type: configmap`, `objectName: recyclarr-configs`,
    `expandObjectName: false`, `mountPath: /config/configs` and `readOnly: true`.
  - Add `workload.main.podSpec.containers.main.envFrom: [{secretRef: {name: decluttarr-secret, expandObjectName: false}}]`.
  - Add `recyclarr-configs.yaml` to the kustomization.
  - This render was checked on 2026-10-17 with chart 15.12.1 and `--kube-version 1.36.5`.
- [ ] **Step 3: Validate.** Run `mise run validate`. Expected: exit 0, and
  `ConfigMap recyclarr-configs is valid`.
- [ ] **Step 4: Commit and open the PR**, but **don't merge yet.**
- [ ] **Step 5: User renames the old config before merging.** Command:
  `sudo -u apps mv /mnt/tank/apps/recyclarr/recyclarr.yml /mnt/tank/apps/recyclarr/recyclarr.yml.bak`.
  Then the user merges.
- [ ] **Step 6: User runs the preview.** Command:
  `kubectl -n media exec deploy/recyclarr -- recyclarr sync --preview`.
  Expected:
  - only `radarr-hd` and `sonarr-hd` are listed;
  - it would create the 2 profiles and their CFs, and delete the stale 2023 CFs;
  - no `[ERR]`.
  Review it with the user before Step 7.
- [ ] **Step 7: User runs the real sync.** Command:
  `kubectl -n media exec deploy/recyclarr -- recyclarr sync`.
  Expected: the profiles `[French MULTi.VO] HD Bluray + WEB` (Radarr) and
  `[French MULTi.VO] HD Bluray + WEB (1080p)` (Sonarr) exist, and `mise run media:stats`
  shows 0 items on them.
- [ ] **Step 8: User deletes the unused 2023 profiles** in the UI: `HD Bluray + WEB` (Radarr)
  and `WEB-1080p` (Sonarr). Both have 0 items (checked on 2026-10-17).
- [ ] **Step 9: Next day,** `kubectl -n media logs deploy/recyclarr --since=24h | grep -c ERR`
  prints `0`.

### Task 3: Canary, then bulk switch (UI, no code)

**Interfaces:** consumes the profiles from Task 2 and `mise run media:stats` from Task 1.

- [ ] **Step 1: User picks the canary items** in the UI (titles stay on their side): 5 movies
  covering VO, MULTi, French-original, 720p and 2160p, plus 2 series (one VO, one with
  720p episodes). Mass Editor → profile = the new HD profile.
- [ ] **Step 2: Immediate check.** On each canary item, Files tab: the CF score is shown.
  Interactive Search: the release Radarr/Sonarr would pick must have VO (MULTi preferred), and
  French-dub-only releases are shown as rejected (`Language: Not Original`). Nothing is grabbed.
- [ ] **Step 3: After 24 h.** `mise run media:stats` shows `grabs_24h` on the same order as the
  baseline, and the canary items show no unexpected grabs in Activity → History.
- [ ] **Step 4: User reviews the no-VO list** (`~/no-vo-list.txt` from
  `scratchpad/no-vo-list.sh`, run by the user). For any item that's mislabeled and really has
  VO, rescan it or fix the language manually before the switch.
- [ ] **Step 5: Bulk switch.** Radarr: Movies → Select all → profile = HD MULTi.VO. Sonarr:
  Series → Select all → profile = HD MULTi.VO (1080p). Jellyseerr: Settings → Services → both
  servers → default profile = the new one.
- [ ] **Step 6: Watch for 2 weeks.** Run `mise run media:stats` daily. **Abort** if `grabs_24h`
  stays above 30 for 3 days: Mass Editor back to the old profiles (the built-ins still exist).
  Also check `zfs list -t snapshot` weekly, and destroy the snapshot early if `tank/media`
  has less than 2 TB free.

### Task 4: radarr-4k app

**Files:**
- Create: `clusters/main/kubernetes/media/radarr-4k/ks.yaml`,
  `clusters/main/kubernetes/media/radarr-4k/app/{helm-release.yaml,kustomization.yaml,radarr-4k.secret.yaml}`
- Modify: `clusters/main/kubernetes/media/kustomization.yaml` (`- radarr-4k/ks.yaml`)

**Interfaces:**
- Produces the secret `radarr-4k-secret` (namespace `media`) with key `RADARR_4K_API_KEY`
  (32 hex chars), consumed by Tasks 5 and 6.
- Produces the Service `radarr-4k`, port 7878, in `media`.

- [ ] **Step 1: User runs the TrueNAS prerequisites.**
  - Create the dataset: `sudo zfs create tank/apps/radarr-4k && sudo chown 568:568 /mnt/tank/apps/radarr-4k`.
  - Create the NFS share: the same `midclt` clone as the jellyseerr share (2026-10-17), from
    `/mnt/tank/apps/radarr`.
  - Create the root folder: `sudo install -d -o 568 -g 568 -m 775 /mnt/tank/media/movies-4k`.
  Verify: `grep -A1 radarr-4k /etc/exports` shows the node IP only.
- [ ] **Step 2: Secret.** Write `radarr-4k.secret.yaml` with
  `stringData.RADARR_4K_API_KEY: <openssl rand -hex 16>`, run `sops -e -i` on it, and check
  `grep -c ENC radarr-4k.secret.yaml` ≥ 1.
- [ ] **Step 3: HelmRelease.** Copy `media/radarr/app/helm-release.yaml`, with these changes:
  - `metadata.name: radarr-4k`
  - host `movies-4k.${DOMAIN_0}` (`auth` middleware kept; no Authelia rule change, so only
    admins get in)
  - config path `/mnt/tank/apps/radarr-4k`
  - an `env` entry `RADARR__AUTH__APIKEY` from `secretKeyRef radarr-4k-secret/RADARR_4K_API_KEY`
  - `ks.yaml` copied from `media/radarr/ks.yaml`, with name and path changed.
- [ ] **Step 4: Validate.** Run `mise run validate`. Expected: exit 0. Then commit and open the
  PR; the user merges.
- [ ] **Step 5: Verify the API key is honored.** Run `source scripts/arr.sh` and then
  `curl -s -o /dev/null -w '%{http_code}' -H "X-Api-Key: $RADARR_4K_API_KEY" localhost:17879/api/v3/system/status`.
  Expected: `200`. If it returns `401`, the env override isn't supported: stop and report.

### Task 5: Wire radarr-4k into the stack

**Files:**
- Modify: `clusters/main/kubernetes/media/recyclarr/app/recyclarr-configs.yaml` (add instance
  `radarr: radarr-uhd`), `clusters/main/kubernetes/media/recyclarr/app/helm-release.yaml`
  (second `envFrom` secretRef `radarr-4k-secret`),
  `clusters/main/kubernetes/media/decluttarr/app/helm-release.yaml` (second `envFrom` and a
  third entry in `RADARR:`, `http://radarr-4k.media.svc.cluster.local:7878` with
  `$(RADARR_4K_API_KEY)`)

**Interfaces:** consumes `radarr-4k-secret` and the `radarr-4k` Service from Task 4.

- [ ] **Step 1: recyclarr instance `radarr-uhd`.** Base it on the template
  `french-multi-vo-uhd-bluray-web.yml`, with
  `base_url: http://radarr-4k.media.svc.cluster.local:7878`,
  `api_key: !env_var RADARR_4K_API_KEY`, profile `92ead7022d13a7858d54e328e6a2f8f9`, and CF
  group `7fc2751eef7e6bdc70b74136e5e35c76` added.
- [ ] **Step 2: decluttarr changes** as listed under Files.
- [ ] **Step 3: Validate.** Run `mise run validate`. Expected: exit 0. Commit, open the PR, the
  user merges. Then the user runs `recyclarr sync --preview` followed by `recyclarr sync` (as in
  Task 2). Expected: the UHD profile exists in radarr-4k, and the decluttarr log shows 3 instances.
- [ ] **Step 4: UI configuration**, done by the user with values from the plan:
  - radarr-4k → Settings → Download Clients: qBittorrent, host `qbittorrent`, port `10095`,
    category `radarr-4k`. Same other settings as radarr's `qbt` client.
  - radarr-4k → Root folder `/media/movies-4k`. Media Management naming: same as radarr.
  - Prowlarr → Settings → Apps: add Radarr, server `http://radarr-4k.media.svc.cluster.local:7878`,
    the API key from `radarr-4k-secret`, then Sync App Indexers.
  - Jellyseerr → Settings → Services → add Radarr, **4K server** ✔, hostname
    `radarr-4k.media.svc.cluster.local`, profile UHD MULTi.VO, root `/media/movies-4k`. Give
    the admin user the "Request 4K" permission only.
  - Emby: new Movies library "Movies 4K" on `/media/movies-4k`.
- [ ] **Step 5: End to end.** The user requests one new movie in 4K in Jellyseerr. Expected,
  within a day: the radarr-4k queue shows a 2160p grab in qBittorrent category `radarr-4k`, the
  file lands in `/media/movies-4k`, and the movie shows up in Emby "Movies 4K".

### Task 6: `migrate-4k` script

**Files:**
- Create: `scripts/migrate_4k.py`, `scripts/tests/test_migrate_4k.py`
- Modify: `mise.toml` (tasks `media:migrate-4k` and `media:search-twins`)

**Interfaces:**
- Consumes `Arr` and `scripts/arr.sh` from Task 1, and `RADARR_4K_API_KEY` from Task 4.
- Produces:
  - `state(radarr_movie: dict | None, radarr4k_movie: dict | None) -> str`, which returns
    one of `"todo" | "moved" | "in-4k" | "done" | "collision" | "skip"`;
  - `migrate_one(radarr: Arr, radarr4k: Arr, tmdb_id: int, hd_profile: int, uhd_profile: int, apply: bool) -> str`
    (it returns the final state);
  - `main(argv)` with `--apply` (default: dry run), `--limit N` (default 10), and subcommand
    `search-twins --limit N`.

Steps per movie, in this order. Each one is idempotent, and `state()` picks where to resume:
1. `PUT /api/v3/movie/editor {movieIds:[id], rootFolderPath:"/media/movies-4k", moveFiles:true}`,
   then poll `GET /api/v3/movie/{id}` until `path` starts with `/media/movies-4k/`
   (timeout 120 s).
2. In radarr-4k: `POST /api/v3/movie` with `tmdbId`, `title`, `year`, `qualityProfileId` (UHD),
   `rootFolderPath:"/media/movies-4k"`, `path` (the moved path), `monitored` (copied from
   radarr), `addOptions.searchForMovie:false`. Then `POST /api/v3/command {name:"RescanMovie", movieId}`,
   and poll until `hasFile` is true (timeout 300 s).
3. Only then, in radarr: `DELETE /api/v3/movie/{id}?deleteFiles=false&addImportExclusion=false`.
4. In radarr: `POST /api/v3/movie` with the same tmdbId, `qualityProfileId` (HD),
   `rootFolderPath:"/media/movies"`, `monitored` (copied), `addOptions.searchForMovie:false`.

The spec put "remove" before "add to radarr-4k". This order is safer: a movie is always in at
least one instance.

- [ ] **Step 1: Write the failing tests** with a fake `Arr` (an in-memory dict of movies and a
  record of calls):
  - `test_selects_only_2160p`: of 3 movies (2160p, 1080p, no file), only the 2160p one is `todo`.
  - `test_dry_run_makes_no_writes`: `apply=False` → no PUT/POST/DELETE recorded.
  - `test_happy_path_order`: calls go PUT editor → POST 4k movie → RescanMovie → DELETE radarr
    (with `deleteFiles=false`) → POST radarr movie; the final state is `done`.
  - `test_resume_after_partial_move`: radarr path already under `/media/movies-4k` and not in
    radarr-4k → resumes at step 2. The movie is in radarr-4k and absent from radarr → resumes at
    step 4. In neither case is the movie absent from both instances at any point.
  - `test_skips_folder_collision`: radarr-4k already has a *different* tmdbId at the target
    path → `collision`, no writes.
  - `test_preserves_monitored_flag`: an unmonitored source → both POST bodies have
    `monitored: False`.
  - `test_search_twins_limit`: of 15 movies in radarr with no file and present in radarr-4k,
    `search-twins --limit 10` posts one `MoviesSearch` with exactly 10 ids.
  - `test_output_has_no_titles`: stdout and stderr contain counts and ids only, never fixture
    titles.
- [ ] **Step 2: Run** `python3 -m unittest discover -s scripts/tests -v`. Expected: the new tests
  FAIL (`ModuleNotFoundError: migrate_4k`).
- [ ] **Step 3: Implement** `migrate_4k.py`. Profile ids are looked up by name from
  `/api/v3/qualityprofile`. Output is counts per state; errors carry tmdb ids only.
- [ ] **Step 4: Run the tests.** Expected: all PASS.
- [ ] **Step 5: Dry run.** `mise run media:migrate-4k`. Expected: `todo: 61`, and 0 in every
  other state (ignoring movies added in Task 5).
- [ ] **Step 6: Commit and open the PR** "feat(media): migrate-4k script". The user merges.

### Task 7: Run the migration

- [ ] **Step 1: First batch of 1.** `mise run media:migrate-4k -- --apply --limit 1`. Expected:
  `done: 1`. In Emby, the movie disappears from Movies and appears in Movies 4K, then check
  whether its watched state survived. If it was lost, tell the user before continuing (spec
  risk 5).
- [ ] **Step 2: Remaining batches.** `--apply --limit 10`, repeated until `todo: 0`. Before each
  batch, `media:stats` shows the radarr-4k file count up by the batch size and radarr's 2160p
  count down by the same.
- [ ] **Step 3: 1080p twins.** `mise run media:search-twins -- --limit 10` once a day until no
  twin is missing (about 7 days). Expected: grabs land in category `radarr`, at 1080p.

### Task 8: Cleanup (30 days after Task 7)

**Files:** Modify `AGENTS.md` (Operations notes: profile layout, radarr-4k, `media:*` tasks).

- [ ] **Step 1: User deletes the unused built-in profiles** in Radarr and Sonarr. Check
  `media:stats` first: 0 items on each.
- [ ] **Step 2: User destroys the snapshot.** Command:
  `sudo zfs destroy tank/media@pre-quality-migration`.
- [ ] **Step 3: Update `AGENTS.md` and memory,** then commit and open the PR.
