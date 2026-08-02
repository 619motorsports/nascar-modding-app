# NTG 2013 career-mode research boundary

The installed PC build does not use its `SPRINTNUMS2012.ARC` content as the
primary season. The recovered retail `BuildSettings.DefaultSeries()` returns
the 2013 series, UID `18306`. In `DB_GAME_LOCAL_SCRIPT.PYC`, that series has
year `2013` and `Locked=False`; the older UID `9670` series has year `2012`
and `Locked=True`.

The installed archives retain the main career components:

- all 36 numbered 2013 calendar events and a 43-driver 2013 roster;
- `GSCAREERCALENDARHELPER`, `GSCAREERFUNCTIONSHELPER`,
  `GSCAREERRACEINTRO`, and `GSCAREERRACESPAWN` bytecode;
- Calendar, Single Season, Chase Contenders, and Career Upgrades interfaces;
- `CAREERNUMBERS.ARC` as the career number bank.

This proves that the asset and database layers are substantially intact.

## Retail frontend trace (2026-08-02)

A read-only Ghidra trace of the installed `NTG2013.exe`, performed against a
disposable copy of the existing project, shows that the Single Season frontend
was not compiled out:

- `FUN_00ae0e30`, the `GSTeamShopMainInterface_c` constructor, builds a menu
  group whose labels include `0x09DF` (`Single Season`), `0x09E0`
  (`Continue Season`), and `0x09DE` (`Restart Season`).
- The `Single Season` label is appended when `DAT_00ef9374 < 0`. That global is
  initialized to `-1`. Its only writes are in command-line parsing for
  `-force_singleplayer [track]`; it is a forced-track override, not a product
  or season lock.
- The group callback is `FUN_00adf690`. With no valid active season driver it
  allocates the 0x1D14-byte `GSSingleSeasonModeInterface_c` through
  `FUN_00ba2df0` and opens the driver/season selection interface. With an
  existing profile it routes directly through `FUN_009c4c10(0x0E, 0)`.
- Selection emits `SINGLE_SEASON_MODE`. `FUN_00adff80` receives that event,
  assigns frontend state `8`, and calls `FUN_00add3f0(1)`.
- `FUN_00add3f0(1)` publishes `SINGLE_SEASON` and performs the Team Shop
  handoff. `RESTART_SINGLE_SEASON` and `SINGLE_SEASON_RESTART` handlers are
  also present.
- The indexed install contains the matching `2DRIVERSELECTTITLE.ARC`,
  `2DRIVERSELECTMENU.ARC`, `2SINGLESEASON.ARC`, career Python, and Team Shop
  resources.
- The main frontend transition registry at `0x009C562C`/`0x009C5641`
  registers IDs 43 and 44 as `TES_DRIVERSELECTTOSINGLESEASON` and
  `TES_SINGLESEASONTODRIVER`.
- The frontend-state jump table maps state 30 to `0x009C69EC`. That handler
  passes the literal `SprintNums2012.arc` to the asynchronous resource loader
  and waits for it before advancing. `SPRINTNUMS2012.ARC` is therefore a
  required boot preload in this retail build, even though its textures depict
  the prior season. It is not an unreferenced bank and must not be deleted,
  renamed, or blindly replaced.
- `CAREERNUMBERS.ARC` has a distinct consumer in the driver-detail UI path;
  the two number banks are not interchangeable based on their names.

The important conclusion is that copying over `SPRINTNUMS2012.ARC` or forcing a
menu boolean is not an evidence-based fix. On the inspected build, the normal
menu condition already passes and the complete entry transition exists. If a
particular launch does not display or complete Single Season, the next useful
evidence is a runtime trace and a disposable retail profile—not a 2012 asset
swap.

## Save boundary

A later Steam Cloud audit found the retail samples missed by the earlier
Documents/AppData search. Steam app `225220` stores them under
`userdata/<steam-user>/225220/remote`:

- `SYS-DATA` is a fixed 524,288-byte slot containing a valid, unencrypted GFS
  envelope. Its payload is 100,310 bytes, version `4.18`, checksum `6,382,838`,
  and begins with the exact 11-byte provider name `PROFILEDATA` followed by
  preamble words `16` and `4`. The remaining 423,958 bytes are zero padding.
- `SYS-TU1.001` is another valid version-`4.18` `PROFILEDATA` envelope with an
  8,436-byte payload and preamble words `0` and `0` (the following core word is
  `3`).
- `SYS-TU2.001` is a valid minimal version-`4.18` `PROFILEDATA` envelope with a
  15-byte payload.
- `liv_save` independently validates the same envelope rules for the custom
  livery slot: 132,912-byte version-`0.12` payload, exact byte-sum checksum,
  and five trailing zero bytes.

The app now includes a read-only GFS parser, including the recovered encrypted
transform, and a native Career page that discovers and validates these slots.
It performs no retail-save writes. This closes the "no on-disk sample" gap and
proves that the documented envelope, fixed-slot padding, `PROFILEDATA` provider
name, and retail version are correct.

A fresh decompilation of the retail save callback also aligns the sample's
first bytes exactly. `FUN_00976FD0` emits the 11-byte `PROFILEDATA` name, then
the two words from profile-object offsets `+0x27F80` and `+0x27F84`, then calls
`FUN_009765F0`. In `SYS-DATA` those words are `16` and `4`, and the first core
word at payload offset `0x13` is `80`; in `SYS-TU1.001` the preamble words are
`0` and `0` and the first core word is `3`. The app can now report the first 40
ordered scalar transfers (124 bytes) before the first dynamic serializer,
using raw object offsets and values only. This is enough to prove byte
alignment, but not enough to assign semantic names or safely synthesize a
career save.

The native GFS envelope, checksum, encryption transform, profile provider,
save/load callbacks, two 0x32188-byte profile objects, 46 ordered scalar
transfers, and several nested serializers have been located in the RE notes.
The app now distinguishes the 40 transfers whose first 124 wire bytes can be
read directly from the six later scalar transfers that occur after the first
dynamic serializer. It reports those later object offsets without inventing
wire offsets or values.

The native Career page also has a read-only before/after comparator for copied
GFS slots. It decrypts both payloads, reports changed byte ranges and changes
inside the proven 40-transfer prefix, and never labels opaque offsets as career
fields. This is intended for the disposable-profile create/race/save/reload
experiment; it does not make ordinary Steam Cloud saves safe to edit.

The comparator now also accepts 2â€“8 ordered copies of the complete `remote`
folder. Each adjacent phase reports added, removed, unchanged, and changed
slots; valid GFS pairs are compared after decryption. Changed bytes are counted
separately for the provider header, two-word profile preamble, proven 124-byte
scalar prefix, and unresolved dynamic payload. This makes a baseline â†’ profile
created â†’ season started â†’ race completed â†’ process restarted â†’ save
reloaded experiment reproducible without assigning meanings to opaque bytes.

For a safe capture, fully exit the game before every copy, place every phase in
a separate disposable directory, and analyze only those copies. Do not select
the live `userdata/<id>/225220/remote` directory as an experiment phase while
the game or Steam synchronization is active. A successful reload is runtime
evidence supplied by the tester; it cannot be inferred from a byte diff alone.
The semantic mapping and full `GSCareerRecords` payload are not complete. The
clean-room `OETXPF01`/`OETXCR01` formats are explicitly not retail-compatible.

For that reason the modding app reports the frontend as present but does not
write an experimental executable or save patch. A recovery action should only
be exposed after a disposable-profile test proves creation, one completed
race, save, reload, and restoration. This avoids corrupting a normal profile
while the native career record semantics remain unresolved.

Evidence source: `H:\OpenETXNascar\docs\re`, especially `mode_catalog.md`,
`profile_save.md`, `game_runtime.md`, `ui_format.md`, and the recovered Python
under `H:\OpenETXNascar\extracted\py`.
