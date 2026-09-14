"""Media Source implementation for DRTV Play.

This exposes DRTV's programme catalogue and live channels through Home
Assistant's Media Source browser, so content can be picked visually
instead of having to know a program name or path up front - the same
experience the plugin.video.drnu Kodi add-on provides.

The navigation logic below mirrors resources/lib/addon.py from
https://github.com/xbmc-danish-addons/plugin.video.drnu, translated from
Kodi's ListItem/xbmcplugin calls to Home Assistant's BrowseMediaSource
tree.

No changes to manifest.json are required for this file to be picked up -
Home Assistant auto-discovers media_source.py in any integration.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from . import DOMAIN, async_get_api
from .video_url_fetch.tvapi import Api, ApiException

_LOGGER = logging.getLogger(__name__)

# DRTV streams are served as HLS.
STREAM_MIME_TYPE = "application/vnd.apple.mpegurl"

# Separator used inside our identifiers. DR paths look like
# "/serie/alene-i-vildmarken_69758", so we avoid "/" as a separator.
SEP = "::"

ROOT_LIVE_TV = "live"
ROOT_MYLIST = "mylist"
ROOT_CONTINUE = "continue"
PREFIX_BROWSE = f"browse{SEP}"
PREFIX_SEASONS = f"seasons{SEP}"
PREFIX_LIST = f"list{SEP}"
PREFIX_PLAY = f"play{SEP}"
PREFIX_PLAY_LIVE = f"playlive{SEP}"

# Entry/item types that never represent something browsable or playable.
SKIP_TYPES = {"ImageEntry", "TextEntry"}
# Item types that are directly playable (as opposed to being a
# programme/series/season that must be browsed further).
PLAYABLE_TYPES = {"program", "episode", "movie"}


async def async_get_media_source(hass: HomeAssistant) -> "DrtvMediaSource":
    """Set up DRTV media source."""
    return DrtvMediaSource(hass)


class DrtvMediaSource(MediaSource):
    """Provide DRTV programmes and live channels as browsable media."""

    name = "DRTV"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def _get_api(self) -> Api:
        """Return the shared Api instance.

        Prefers an account set up via the config flow (Configuration ->
        Integrations -> DRTV Play -> Add Entry), which is what makes "My
        List" and "Continue watching" available, and falls back to a
        cached anonymous session for plain YAML setups.
        """
        return await async_get_api(self.hass)

    @staticmethod
    def _is_logged_in(api: Api) -> bool:
        return bool(api.logged_in)

    # ------------------------------------------------------------------
    # Resolving
    # ------------------------------------------------------------------

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a media item to a playable URL."""
        api = await self._get_api()
        identifier = item.identifier or ""

        try:
            if identifier.startswith(PREFIX_PLAY):
                video_id = identifier[len(PREFIX_PLAY):]
                stream = await self.hass.async_add_executor_job(api.get_stream, video_id)
                if not stream or not stream.get("url"):
                    raise Unresolvable(f"DRTV did not return a stream for id {video_id}")
                return PlayMedia(stream["url"], STREAM_MIME_TYPE)

            if identifier.startswith(PREFIX_PLAY_LIVE):
                title = identifier[len(PREFIX_PLAY_LIVE):]
                channel = await self.hass.async_add_executor_job(
                    self._find_channel, api, title
                )
                if channel is None:
                    raise Unresolvable(f"Unknown DRTV channel: {title}")
                url = await self.hass.async_add_executor_job(
                    api.get_channel_url, channel, False
                )
                if not url:
                    raise Unresolvable(f"DRTV did not return a stream for channel {title}")
                return PlayMedia(url, STREAM_MIME_TYPE)
        except ApiException as err:
            raise Unresolvable(str(err)) from err

        raise Unresolvable(f"Unknown media identifier: {identifier}")

    @staticmethod
    def _find_channel(api: Api, title: str) -> dict[str, Any] | None:
        for channel in api.getLiveTV():
            if channel.get("title", "").lower() == title.lower():
                return channel
        return None

    # ------------------------------------------------------------------
    # Browsing
    # ------------------------------------------------------------------

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse DRTV media."""
        api = await self._get_api()
        identifier = item.identifier or ""

        try:
            if identifier == "":
                return await self.hass.async_add_executor_job(self._browse_root, api)

            if identifier == ROOT_LIVE_TV:
                return await self.hass.async_add_executor_job(self._browse_live, api)

            if identifier == ROOT_MYLIST:
                return await self.hass.async_add_executor_job(self._browse_mylist, api)

            if identifier == ROOT_CONTINUE:
                return await self.hass.async_add_executor_job(self._browse_continue, api)

            if identifier.startswith(PREFIX_BROWSE):
                quoted_title, _, path = identifier[len(PREFIX_BROWSE):].partition(SEP)
                return await self.hass.async_add_executor_job(
                    self._browse_path, api, path, False, identifier, unquote(quoted_title)
                )

            if identifier.startswith(PREFIX_SEASONS):
                quoted_title, _, path = identifier[len(PREFIX_SEASONS):].partition(SEP)
                return await self.hass.async_add_executor_job(
                    self._browse_path, api, path, True, identifier, unquote(quoted_title)
                )

            if identifier.startswith(PREFIX_LIST):
                quoted_title, _, rest = identifier[len(PREFIX_LIST):].partition(SEP)
                list_id, _, param = rest.partition(SEP)
                return await self.hass.async_add_executor_job(
                    self._browse_list, api, list_id, param or "NoParam", identifier, unquote(quoted_title)
                )
        except ApiException as err:
            raise BrowseError(str(err)) from err

        raise BrowseError(f"Unknown media identifier: {identifier}")

    # -- individual browse "pages" (all synchronous - run in executor) --

    def _browse_root(self, api: Api) -> BrowseMediaSource:
        """Build the top-level menu: Live TV plus DRTV's own front page."""
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=ROOT_LIVE_TV,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.VIDEO,
                title="Live TV",
                can_play=False,
                can_expand=True,
            )
        ]

        if self._is_logged_in(api):
            # Only shown for a DRTV account set up through the config
            # flow - matches the Kodi add-on only offering "My List" and
            # "Continue watching" once you've logged in.
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=ROOT_CONTINUE,
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=f"Continue watching ({api.user_name})",
                    can_play=False,
                    can_expand=True,
                )
            )
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=ROOT_MYLIST,
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=f"My List ({api.user_name})",
                    can_play=False,
                    can_expand=True,
                )
            )

        for entry in api.get_home():
            path = entry.get("path")
            title = entry.get("title")
            if not path or not title:
                continue
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{PREFIX_BROWSE}{quote(title, safe='')}{SEP}{path}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=title,
                    can_play=False,
                    can_expand=True,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type="",
            title="DRTV",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    def _browse_live(self, api: Api) -> BrowseMediaSource:
        """List the DRTV live channels as playable items."""
        children = []
        for channel in api.getLiveTV():
            title = channel.get("title")
            if not title:
                continue
            image = channel.get("item", {}).get("images", {}).get("logo")
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{PREFIX_PLAY_LIVE}{title}",
                    media_class=MediaClass.CHANNEL,
                    media_content_type=MediaType.VIDEO,
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=image,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=ROOT_LIVE_TV,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title="Live TV",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.CHANNEL,
            children=children,
        )

    def _browse_mylist(self, api: Api) -> BrowseMediaSource:
        """List the signed-in account's saved (bookmarked) programmes."""
        items = api.get_mylist()
        children = [
            child
            for raw in items
            if (child := self._item_to_browse(api, raw, False)) is not None
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=ROOT_MYLIST,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title="My List",
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_continue(self, api: Api) -> BrowseMediaSource:
        """List the signed-in account's continue-watching queue."""
        items = api.get_continue()
        children = [
            child
            for raw in items
            if (child := self._item_to_browse(api, raw, False)) is not None
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=ROOT_CONTINUE,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title="Continue watching",
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_path(
        self, api: Api, path: str, seasons: bool, identifier: str, title: str
    ) -> BrowseMediaSource:
        """List the contents of a DRTV path.

        Mirrors DrDkTvAddon.list_entries() from the Kodi add-on: a path's
        "programcard" either lists several entries directly, or resolves
        to a single season (show more seasons, or its episodes) or a
        single list of related items.

        `title` is the friendly name already shown for this folder in its
        parent listing (carried in the identifier) - reusing it keeps the
        browser header/breadcrumb consistent instead of trying to
        re-derive a name from the API response for every page load.
        """
        card = api.get_programcard(path)
        entries = card.get("entries") or []
        items: list[dict[str, Any]] = []
        force_seasons = False

        if not entries:
            # Some pages (e.g. certain /liste/<id> paths) come back with no
            # entries, but the same id works as a recommendations list.
            try:
                list_id = int(path.rstrip("/").split("/")[-1])
                items = api.get_recommendations(list_id).get("items", [])
            except (ValueError, ApiException):
                items = []
        elif len(entries) > 1:
            items = entries
        else:
            entry = entries[0]
            entry_type = entry.get("type")
            if entry_type == "ItemEntry":
                detail = entry.get("item", {})
                if detail.get("type") == "season":
                    show = detail.get("show", {})
                    if seasons or show.get("availableSeasonCount", 1) == 1:
                        items = detail.get("episodes", {}).get("items", [])
                    else:
                        items = show.get("seasons", {}).get("items", [])
                        force_seasons = True
                else:
                    items = [entry]
            elif entry_type == "ListEntry":
                items = api.unfold_list(entry["list"])
            else:
                items = [entry]

        children = [
            child
            for raw in items
            if (child := self._item_to_browse(api, raw, force_seasons)) is not None
        ]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title or path,
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_list(
        self, api: Api, list_id: str, param: str, identifier: str, title: str
    ) -> BrowseMediaSource:
        """List the contents of a DRTV "list" (id + parameter, no path)."""
        raw_list = api.get_list(list_id, param)
        items = api.unfold_list(raw_list)
        children = [
            child
            for raw in items
            if (child := self._item_to_browse(api, raw, False)) is not None
        ]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title or raw_list.get("title", "DRTV"),
            can_play=False,
            can_expand=True,
            children=children,
        )

    # -- helpers --

    def _item_to_browse(
        self, api: Api, raw: dict[str, Any], force_seasons: bool
    ) -> BrowseMediaSource | None:
        """Convert one DRTV entry/item into a BrowseMediaSource, or None to skip it.

        `raw` may either be a "flat" item (an episode, season or program
        straight from an unfolded list) or an "entry" wrapper that carries
        navigation fields (id/path/title/images) at the top level and the
        actual programme details nested under "item". Both shapes are
        handled the same way the Kodi add-on's kodi_item() does.
        """
        top_type = raw.get("type")
        if top_type in SKIP_TYPES:
            return None

        detail = raw.get("item", raw)
        detail_type = detail.get("type", top_type)

        title = raw.get("title") or detail.get("title")
        if not title:
            return None

        try:
            title = api.get_info(detail)[0]
        except (KeyError, TypeError):
            pass

        images = detail.get("images") or raw.get("images") or {}
        thumbnail = None
        for label in ("tile", "poster", "square"):
            if images.get(label):
                thumbnail = images[label]
                break

        is_folder = detail_type not in PLAYABLE_TYPES
        path = raw.get("path") or detail.get("path")
        if path and str(path).startswith("/kanal/"):
            is_folder = False

        if is_folder:
            if path:
                prefix = PREFIX_SEASONS if force_seasons and detail_type == "season" else PREFIX_BROWSE
                identifier = f"{prefix}{quote(title, safe='')}{SEP}{path}"
            else:
                list_info = raw.get("list") or detail.get("list")
                if not list_info or not list_info.get("id"):
                    return None
                param = list_info.get("parameter", "NoParam")
                identifier = f"{PREFIX_LIST}{quote(title, safe='')}{SEP}{list_info['id']}{SEP}{param}"

            media_class = MediaClass.DIRECTORY
            if detail_type == "season":
                media_class = MediaClass.SEASON
            elif detail_type in ("program", "show"):
                media_class = MediaClass.TV_SHOW

            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=identifier,
                media_class=media_class,
                media_content_type=MediaType.VIDEO,
                title=title,
                can_play=False,
                can_expand=True,
                thumbnail=thumbnail,
            )

        # Playable item.
        video_id = raw.get("id") or detail.get("id")
        if not video_id:
            return None

        media_class = MediaClass.EPISODE if detail_type == "episode" else MediaClass.MOVIE
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{PREFIX_PLAY}{video_id}",
            media_class=media_class,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=True,
            can_expand=False,
            thumbnail=thumbnail,
        )
