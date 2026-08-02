"""Canonical metadata for every supported Eutechnyx NASCAR game."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class GameProfile:
    id: str
    name: str
    short_name: str
    folder_names: tuple[str, ...]
    required_archives: tuple[str, ...]
    paint_primary_archive: str
    tabs: tuple[str, ...]
    paint_modes: tuple[str, ...]
    full_feature_set: bool
    content_season: int
    season_prefix: str
    number_container: str
    livery_style: str
    dormant_seasons: tuple[int, ...]
    dormant_number_containers: tuple[str, ...]
    unavailable_menu_keys: tuple[str, ...]
    ai_profiles_file: str
    data_subdir: str
    team_editor_mode: str
    graphics_mode: str
    body_models: tuple[tuple[str, str], ...]
    car_manufacturers: tuple[tuple[int, str], ...]
    series_uid: int | None = None

    def as_legacy_dict(self) -> dict:
        value = asdict(self)
        value['season_year'] = value['content_season']
        return value


_COMMON_TABS = (
    'Setup', 'Grid', 'Names', 'Text', 'Stats', 'Audio', 'Race', 'AI',
    'UI', 'Settings',
)


PROFILES = {
    'nascar13': GameProfile(
        id='nascar13',
        name='NASCAR The Game: 2013',
        short_name='NTG 2013',
        folder_names=('NASCAR The Game 2013', 'NASCAR The Game: 2013'),
        required_archives=('0', '7', '8'),
        paint_primary_archive='7',
        tabs=_COMMON_TABS,
        paint_modes=('library',),
        full_feature_set=False,
        # BuildSettings.DefaultSeries() selects UID 18306, the unlocked 2013
        # series. UID 9670 and SPRINTNUMS2012 are locked/dormant NAS2 content.
        content_season=2013,
        season_prefix='13',
        number_container='CAREERNUMBERS.ARC',
        livery_style='ntg2013_dlc',
        dormant_seasons=(2012,),
        dormant_number_containers=('SPRINTNUMS2012.ARC',),
        unavailable_menu_keys=('shoplogo2',),
        ai_profiles_file='ai_profiles_nascar13.csv',
        data_subdir='nascar13',
        team_editor_mode='names_only',
        graphics_mode='discovered',
        body_models=(('0', 'NASCAR4_BODY0_0.ARC'), ('0', 'NASCAR3_BODY0_0.ARC')),
        car_manufacturers=((0, 'Chevrolet'), (1, 'Dodge'), (2, 'Ford'), (3, 'Toyota')),
        series_uid=18306,
    ),
    'nascar14': GameProfile(
        id='nascar14',
        name="NASCAR '14",
        short_name="NASCAR '14",
        folder_names=("NASCAR '14", 'NASCAR 14'),
        required_archives=('0', '7', '8'),
        paint_primary_archive='7',
        tabs=_COMMON_TABS,
        paint_modes=('library',),
        full_feature_set=False,
        content_season=2014,
        season_prefix='14',
        number_container='SPRINTNUMS2014.ARC',
        livery_style='numbered_season',
        dormant_seasons=(),
        dormant_number_containers=(),
        unavailable_menu_keys=('shoplogo2',),
        ai_profiles_file='ai_profiles_nascar14.csv',
        data_subdir='nascar14',
        team_editor_mode='names_only',
        graphics_mode='discovered',
        body_models=(
            ('0', 'NASCAR5_BODY0_0.ARC'),
            ('0', 'NASCAR4_BODY0_0.ARC'),
            ('0', 'NASCAR3_BODY0_0.ARC'),
        ),
        car_manufacturers=((0, 'Chevrolet'), (1, 'Dodge'), (2, 'Ford'), (3, 'Toyota')),
        series_uid=22538,
    ),
    'nascar15': GameProfile(
        id='nascar15',
        name='NASCAR 15',
        short_name='NASCAR 15',
        folder_names=('NASCAR 15',),
        required_archives=('0', '2'),
        paint_primary_archive='2',
        tabs=_COMMON_TABS + ('Repoint', 'Checkup'),
        paint_modes=('library', 'create', 'schedule'),
        full_feature_set=True,
        content_season=2015,
        season_prefix='15',
        number_container='SPRINTNUMS2015.ARC',
        livery_style='numbered_season',
        dormant_seasons=(),
        dormant_number_containers=(),
        unavailable_menu_keys=(),
        ai_profiles_file='ai_profiles.csv',
        data_subdir='',
        team_editor_mode='full',
        graphics_mode='packaged',
        body_models=(('3', 'NASCAR6_BODY0_0.ARC'),),
        car_manufacturers=((0, 'Chevrolet'), (2, 'Ford'), (3, 'Toyota')),
        series_uid=25040,
    ),
}


GAME_PROFILES = {key: profile.as_legacy_dict() for key, profile in PROFILES.items()}


def get_profile(game_id: str) -> GameProfile:
    try:
        return PROFILES[game_id]
    except KeyError as exc:
        raise ValueError(f'unsupported game profile: {game_id}') from exc
