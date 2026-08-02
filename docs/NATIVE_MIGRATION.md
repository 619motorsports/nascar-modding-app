# Native workflow migration

The native Qt application is the default frontend. Editing code belongs under
`nascar_modding/editing`; Qt pages and the legacy Flask compatibility routes
may validate request/UI shape, but they must not implement archive writes.

## Shared-service ownership

| Workflow | Shared owner | Native page | Flask state |
|---|---|---|---|
| Exact archive entry and append/repoint | `ArchiveEntryEditor` / `ResourceEditor` | Archive Resources | Delegated |
| Existing stock-paint smart image install | `StockPaintEditor` / `NativeLiveryWrapperEditor` | Paint Assets | Transactional SD/HD mip install, optional Paint Booth alpha mask, backup, rollback, and read-back delegated |
| Paired backups and full restore | `BackupManager` | Backups & Restore | Delegated |
| Driver display names and card handles | `DriverNameEditor` / `DriverHandleEditor` | Driver Names | Exact language-table names and length-changing Python-2 handle strings delegated with persistent identity and read-back |
| Interface/localization text | `TextTableEditor` | Interface Text | Delegated |
| Driver AI ratings | `RatingsEditor` | AI Ratings | Delegated |
| Player/AI SCR physics | `ScrEditor` | Racing Controls | Delegated; old duplicate writers removed |
| Python-2 database scalar fields | `PycRecordEditor` | Game Database | Delegated; old exact-field writers removed |
| Decoded ARCC texture banks | `TextureBankEditor` | Images & Textures | Menu, number-card, and Driver Select routes delegated |
| 36-race schedule order | `ScheduleEditor` plus the recovered schedule helper | Season Schedule | Advanced custom-event route still uses the same recovered helper |
| Custom/repeated schedule events and lap profiles | `ScheduleEditor` plus recovered visible/runtime-link helpers | Season Schedule | Delegated |
| FSB5/SND audio banks and managed FFmpeg | `AudioBankEditor` / `AudioToolsManager` / `formats.audio` | Audio Banks | Delegated; old route-local parsers, writers, and installer removed |
| Track inventory, comparison, and export | `TrackInventory` | Track Files | Delegated |
| AI presets and pit observations | `UserLibrary` | Presets & Pit Notes | Delegated |
| App-owned data export/import | `AppDataManager` | App Data Transfer | Delegated; allowlisted atomic restore with recovery ZIP |
| Installation checks and diagnostics | `SupportReporter` | Checkup & Support | Delegated; diagnostics exclude archive payloads |
| NASCAR 15 teams and presentation assets | `TeamEditor` / `TeamPresentationEditor` / `TeamPresentationRecovery` | Teams & Manufacturers | Driver moves, manufacturers, names, reserve-team preparation, logos, Driver Select art/repair, saved-link repair, and exact undo delegated |
| Managed paint creation, catalog, UID diagnostics, and named-race AI assignments | `ManagedPaintEditor` / `NativeLiveryWrapperEditor` | Managed Paints & AI | Creation, conservative live-slot adoption/source recovery, UID-pool inspection/verdict state, AI assignment/save/preview/apply/restore, library export, exact latest-create removal, and delete/undo redo delegated; SD/HD output is byte-matched against the legacy proven writer |
| Multi-archive rollback/checkpoints | `AppendRepointTransaction`, `ManagedPaintTransaction`, `ManagedPaintCheckpoint`, `TeamAssetTransaction`, `TeamAssetCheckpoint` | Used by managed paint/team/full-repair workflows | Exact archive/index, multiple in-place regions, sidecar recovery, durable manifests, and paint-state age guards delegated; synthetic round-trip coverage |
| Composite season packs | `SeasonPackEditor` / `ExactFileTransaction` | Composite Season Packs | Shared v3 export, validation/preview, selected-category import, driver-name/handle identity conversion from legacy v1, and exact aggregate rollback delegated; duplicate v2 Flask backend removed |
| Failure-focused whole-install repair | `FullRepairEditor` / `ExactFileTransaction` | Full Repair | Read-only dependency scan, reconstructable managed-paint/team repair, whole-file rollback, semantic verification, and report export delegated; old Flask repair backend removed |
| NTG2013 frontend/read-only retail saves | Career audit / GFS parser and transition analyzer | NTG2013 Career | No writer is exposed; hard-coded `SPRINTNUMS2012` preload is preserved; 2â€“8 ordered copied-save phases can be compared read-only by structural payload region |

## Legacy compatibility UI

All known write workflows now cross shared editing services and have a native Qt
surface. The Flask runtime remains an explicit compatibility UI, but no longer
owns a separate implementation of a migrated writer.

The generic Archive Resources page already provides a safe raw fallback for
unmigrated resource types. That does not make format-aware writes safe: a
native page should only enable them after their parser, diff guard, backup,
transaction, and semantic read-back live in a shared service.
