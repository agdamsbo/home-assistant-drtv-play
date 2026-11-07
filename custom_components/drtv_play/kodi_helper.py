"""Helpers for playing DRTV content on Kodi players.

Why this exists
---------------
DRTV's on-demand streams are fragmented-MP4 HLS playlists
(``.../stream_fmp4/master_manifest.m3u8``). Kodi's built-in FFmpeg player
cannot open them (it hangs in "Creating Demuxer" and then fails with
"OpenDemuxStream - Error creating demuxer"), whereas Cast devices play them
fine. Kodi needs ``inputstream.adaptive`` to be selected, which can only be
done through the list item an add-on resolves - and Home Assistant's Kodi
integration only passes a bare URL.

The ``plugin.video.drnu`` Kodi add-on does select ``inputstream.adaptive``
when it resolves a video, so for Kodi players we hand Kodi a ``plugin://``
URL pointing at that add-on instead of the raw stream URL, and let it
resolve and play the programme itself.

Requirement: ``plugin.video.drnu`` must be installed on the Kodi device.
"""
from __future__ import annotations

from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

KODI_PLATFORM = "kodi"
KODI_DRNU_ADDON_ID = "plugin.video.drnu"


def is_kodi_entity(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Return True if ``entity_id`` is a media player from the Kodi integration."""
    if not entity_id:
        return False
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == KODI_PLATFORM


def build_kodi_play_url(video_id: str | int, path: str | None = None) -> str:
    """Build the plugin URL that makes the Kodi DR add-on play a programme.

    Mirrors the URL the add-on builds itself for playable items:
    ``plugin://plugin.video.drnu/?playVideo=<id>&kids=False&idpath=<path>``.
    ``path`` (e.g. ``/episode/some-title_123``) is only used by the add-on for
    its "recently watched" list, but the add-on expects the parameter to be
    present, so it is always included.
    """
    return (
        f"plugin://{KODI_DRNU_ADDON_ID}/"
        f"?playVideo={video_id}&kids=False&idpath={quote(path or '', safe='/')}"
    )


def build_kodi_live_url(channel_id: str | int) -> str:
    """Build the plugin URL that makes the Kodi DR add-on play a live channel.

    The add-on's ``playVideo`` handler treats an ``idpath`` starting with
    ``/kanal`` as a live channel: it resolves the channel's current stream and
    plays it with ``inputstream.adaptive``, like on-demand videos. Subtitles
    follow the add-on's own "live TV subtitles" setting.
    """
    return build_kodi_play_url(channel_id, f"/kanal/{channel_id}")
