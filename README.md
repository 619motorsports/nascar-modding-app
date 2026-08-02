# NASCAR Modding App

A native Windows desktop modding tool for **NASCAR The Game: 2013**, **NASCAR '14**, and **NASCAR 15**.

The desktop app currently provides verified installation discovery, primary-season paint-container management, decoded image/texture-bank export and guarded replacement, generic indexed-resource export/repoint/package tools, paired pristine backup/restore, driver-name editing, interface-text editing, driver AI ratings, Player/AI SCR racing controls, exact-field game-database editing (race laps, AI behavior, and world pace), verified 36-race season reordering, native OpenGL 3D car previews, and live archive/mapping audits. All migrated write workflows use shared backend services with pristine backups, read-back verification, and rollback on failure.

## Setup

1. Download the `NASCARModdingApp-Windows-x64` artifact from the latest successful **Build Windows EXE** GitHub Actions run.
2. Extract the entire ZIP somewhere outside OneDrive.
3. Run `NASCARModdingApp.exe`. The packaged build does not require Python.
4. On Setup, choose the game and its installation folder (the folder containing `data`).

Source checkouts can still run `START_APP.bat`. It now probes each interpreter directly and supports Python 3.10 or newer, including Python 3.13.

## Building the Windows app

Install Python 3.13, then run:

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python -m unittest discover -s tests -v
python -m PyInstaller --noconfirm --clean NASCARModdingApp.spec
dist\NASCARModdingApp\NASCARModdingApp.exe --smoke-test
```

The distributable folder is `dist\NASCARModdingApp`. Keep the whole folder together; the EXE uses the bundled Qt libraries, game mappings, and reverse-engineered helper modules inside it. The GitHub Actions workflow builds and smoke-tests this folder on every push to `main` or `master`, every pull request to those branches, version tags, and manual runs, then uploads a ZIP artifact.

The known Steam folder names are detected automatically. You can also browse to a nonstandard install.

## Native workflows

- Paint Assets: inspect, export, exact-size import, and restore primary-season raw paint containers.
- Driver Names: edit exact LDA string indexes across every matching language table.
- AI Ratings: edit the seven mapped Python-2 AI constants for each primary roster driver.
- Images & Textures: distinguish allocation-padded single-level A8R8G8B8/A1R5G5B5/DXT surfaces from tightly packed livery and car-material mip chains, without allowing source image dimensions to resize the desktop window.
- 3D Cars: switch between the installed Chevrolet, Dodge, Ford, and Toyota geometry variants (as available per game), preview wheels on the game-authored attachment points, render stock chassis/glass/tire/detail textures from the matching game material pack, and preview either an installed paint or an external image.
- Navigation: find any migrated tool from the searchable sidebar (`Ctrl+K`), retain the last open page, and keep the selected game visible throughout the app.
- Mapping Audit: validate every installed CDF index, payload boundary, body model, number bank, database, and season mapping.

NASCAR The Game: 2013 uses its active 2013 series (UID 18306). Dormant 2012 resources such as `SPRINTNUMS2012.ARC` are deliberately excluded from primary-season editing.

## Legacy compatibility interface

Run `START_LEGACY_WEB_APP.bat` only when you need an advanced workflow that has not yet moved to the desktop UI, such as audio, duplicate/custom schedule event substitution, or team-asset tools. It is no longer the default application. Its driver-name, interface-text, AI-rating/game-database, SCR racing-control, decoded menu/number/driver-select texture, generic resource/repoint, and backup routes delegate to the same shared editing services as the native UI.

## Safety

Close the game before applying or restoring changes. The app creates a pristine backup beside each archive/index before its first write. Variable-size resources are appended and their CDF record is atomically repointed; failures restore the prior archive/index state.

The first dependency install needs internet access. Normal use has no telemetry or update check and works offline.

Career-mode research for NTG 2013 is documented in `docs/NTG2013_CAREER_RESEARCH.md`. The dormant career data/assets are present, but no retail-save-compatible frontend transition has been proven safe, so the app does not apply a speculative executable or save patch.
