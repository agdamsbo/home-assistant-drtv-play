import logging
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .video_url_fetch.tvapi import Api, ApiAuthException, ApiException, pick_thumbnail

DOMAIN = "drtv_play"

DEPENDENCIES = ['media_player']

CONF_ENTITY_ID = 'entity_id'
CONF_PROGRAM_NAME = 'program_name'
CONF_CHANNEL = 'channel'
CONF_SUBTITLES = 'subtitles'

SERVICE_PLAY_LATEST = 'play_latest'
SERVICE_PLAY_LATEST_SCHEMA = vol.Schema({
    CONF_ENTITY_ID: cv.entity_ids,
    CONF_PROGRAM_NAME: str
})

SERVICE_PLAY_CHANNEL = 'play_channel'
SERVICE_PLAY_CHANNEL_SCHEMA = vol.Schema({
    CONF_ENTITY_ID: cv.entity_ids,
    CONF_CHANNEL: str,
    CONF_SUBTITLES: bool,
})

_LOGGER = logging.getLogger(__name__)

# Key used in hass.data[DOMAIN] for a lazily created, YAML-only anonymous
# Api instance, so repeated service calls reuse the same tokens instead of
# logging in anonymously again on every call.
_ANON_KEY = "_anonymous_api"


async def async_get_api(hass) -> Api:
    """Return a shared Api instance.

    Prefers a logged-in account set up via the config flow (My List /
    Continue Watching, higher stream quality) over an anonymous session.
    Falls back to a cached anonymous Api for pure-YAML setups.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})

    logged_in = [
        entry_data["api"]
        for entry_data in domain_data.values()
        if isinstance(entry_data, dict) and entry_data.get("api") and entry_data.get("logged_in")
    ]
    if logged_in:
        return logged_in[0]

    any_entry = [
        entry_data["api"]
        for entry_data in domain_data.values()
        if isinstance(entry_data, dict) and entry_data.get("api")
    ]
    if any_entry:
        return any_entry[0]

    if _ANON_KEY not in domain_data:
        domain_data[_ANON_KEY] = await hass.async_add_executor_job(Api)
    return domain_data[_ANON_KEY]


def _channel_metadata(channel):
    """Build a friendly title/thumbnail for a live channel.

    Uses the currently-airing programme (from the schedule string already
    fetched by getLiveTV()) rather than just the bare channel name, so the
    media player shows e.g. "DR1 - TV Avisen" instead of just "DR1".
    """
    title = channel.get("title", "DRTV")
    schedule = (channel.get("schedule_str") or "").strip()
    if schedule:
        first_line = schedule.splitlines()[0].strip()
        # Lines look like "20:00 Some Programme Title" - drop the leading
        # HH:MM if present.
        _, _, now_playing = first_line.partition(" ")
        now_playing = now_playing.strip() or first_line
        if now_playing:
            title = f"{title} - {now_playing}"
    thumb = pick_thumbnail(channel.get("item", {}).get("images"), preferred=("logo", "tile", "poster", "square"))
    return title, thumb


async def async_setup(hass, config):

    async def play_latest(service):
        """Play the latest svt play video from a specified program"""

        entity_id = service.data.get(CONF_ENTITY_ID)
        program_name = service.data.get(CONF_PROGRAM_NAME)

        def fetch_video_url(api):
            item = api.get_latest(program_name)
            url = ''
            if item:
                url = api.get_stream(item['id'])['url']
            return url, item
        api = await async_get_api(hass)
        url, item = await hass.async_add_executor_job(fetch_video_url, api)
        if url:
            await hass.services.async_call('media_player', 'play_media', {
                'entity_id': entity_id,
                'media_content_id': url,
                'media_content_type': 'video',
                'extra': {
                    'title': item.get('title', program_name),
                    'thumb': pick_thumbnail(item.get('images')),
                }
            })
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_LATEST, play_latest, SERVICE_PLAY_LATEST_SCHEMA
    )

    async def play_channel(service):
        """Play the specified channel"""

        entity_id = service.data.get(CONF_ENTITY_ID)
        channel = service.data.get(CONF_CHANNEL)
        subtitles = service.data.get(CONF_SUBTITLES)

        def fetch_video_url(api):
            channels = api.getLiveTV()

            url = None
            metadata = None
            for item in channels:
                if item['title'].lower() == channel.lower():
                    url = api.get_channel_url(item, with_subtitles=subtitles)
                    metadata = _channel_metadata(item)
            return url, metadata
        api = await async_get_api(hass)
        video_url, metadata = await hass.async_add_executor_job(fetch_video_url, api)

        if video_url:
            title, thumb = metadata
            await hass.services.async_call('media_player', 'play_media', {
                'entity_id': entity_id,
                'media_content_id': video_url,
                'media_content_type': 'video',
                'extra': {
                    'title': title,
                    'thumb': thumb,
                }
            })
        else:
            _LOGGER.error("Unknown DRTV channel: %s", channel)
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_CHANNEL, play_channel, SERVICE_PLAY_CHANNEL_SCHEMA
    )

    return True


async def async_setup_entry(hass, entry):
    """Set up DRTV Play from a config entry (anonymous or logged in)."""
    username = entry.data.get(CONF_USERNAME) or None
    password = entry.data.get(CONF_PASSWORD) or None

    try:
        api = await hass.async_add_executor_job(Api, username, password)
    except ApiAuthException as err:
        # Bad/expired credentials - prompt the user to log in again rather
        # than retrying forever, matching how other cloud integrations
        # handle a stale login.
        raise ConfigEntryAuthFailed(f"DRTV login failed: {err}") from err
    except ApiException as err:
        raise ConfigEntryNotReady(f"Could not reach DRTV: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "logged_in": bool(username),
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass, entry):
    """Reload the entry when its options/credentials change (e.g. after
    updating username/password through the Options flow)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    """Unload a DRTV Play config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
