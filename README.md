[![unit testing in python](https://github.com/TermeHansen/home-assistant-drtv-play/actions/workflows/python-package-conda.yml/badge.svg)](https://github.com/TermeHansen/home-assistant-drtv-play/actions/workflows/python-package-conda.yml)

# Home Assistant DRTV Play

Play DRTV videos and channels via Home Assistant, with a browsable Media Source and optional DRTV account login.

## Media Source (browse DRTV visually)

Once installed, "DRTV" appears as a source in Home Assistant's Media browser (Settings → Media, or the media player's browse button). You can navigate DRTV's catalogue and Live TV channels without knowing a program name up front, similar to the Kodi add-on's directory listing.

If you log in with a DRTV account (see Setup below), two extra folders appear: **My List** and **Continue watching**, synced from your account.

## Setup

1. Install via [HACS](https://hacs.xyz/) (add this repo as a custom repository), or copy `custom_components/drtv_play` into `<home assistant config>/custom_components/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → DRTV Play**.
4. Leave the email/password fields blank for anonymous browsing, or enter your DRTV login to enable My List and Continue watching.

Credentials can be changed later from the integration's **Configure** button, and Home Assistant will prompt you to log in again if a saved login stops working.

The legacy YAML setup (`drtv_play:` in `configuration.yaml`) still registers the services below, but the config flow above is required for Media Source browsing and account login.

## Available actions

### Play Latest
Play the latest episode from a specific show. Search by any string (e.g. "gurli"), by the ending path from https://www.dr.dk/drtv/ (e.g. "/serie/alene-i-vildmarken_69758"), or by id (e.g. 69758).
```yaml
- service: drtv_play.play_latest
  entity_id: media_player.living_room_tv
  data:
    program_name: gurli
```

### Play Channel
Play one of the DRTV channels. Available channels are DR1, DR2, DRTV, DRTV Ekstra and DR Ramasjang.
```yaml
- service: drtv_play.play_channel
  entity_id: media_player.living_room_tv
  data:
    channel: dr2 # Available channels: dr1, dr2, drtv, drtv ekstra, dr ramasjang
    subtitles: false # Optional, default is false
```

### Use in automations
```yaml
automation:
- alias:
  trigger:
  # Some trigger
  action:
  - service: drtv_play.play_channel
    entity_id: media_player.living_room_tv
    data:
      channel: dr2
```

## Get the `program_name` field

1. Search and click on the program you want at [dr.dk/tv](https://www.dr.dk/tv)
2. Provide the program name, or the program identifier from the url.

## Inspiration

This add-on draws heavily on the [plugin.video.drnu for XBMC/Kodi](https://github.com/TermeHansen/plugin.video.drnu) as well as the [home-assistant-svt-play addon](https://github.com/lindell/home-assistant-svt-play). Note that `plugin.video.drnu` is GPLv2-licensed while this repo is MIT; the reused catalogue/login logic predates and continues under that mismatch.

## AI-assisted development disclosure

The Media Source browser, the DRTV account login (config flow, reauth, and the `full_login`/token-refresh code in `tvapi.py`), and the Options flow were written with [Claude](https://www.anthropic.com/claude) (Anthropic), working from this repository, the `plugin.video.drnu` Kodi add-on's current source, and Home Assistant's developer documentation. Specifically, Claude:

- Ported the login/OIDC handshake and My List/Continue Watching endpoints from `plugin.video.drnu`'s `tvapi.py` into this component's `tvapi.py`.
- Wrote `media_source.py`, `config_flow.py`, `strings.json`, and the config-entry changes to `__init__.py` and `manifest.json` from scratch.
- Verified the new code compiles and unit-tested the token-parsing logic in isolation, but **could not run the login flow or media browsing against DR's live servers** (no network access to `dr-massive.com`/`login.dr.dk` from the assistant's sandbox).

Treat this code with the same scrutiny you'd give any first-draft AI-generated PR: review it, and please open an issue if you hit a bug against the real DR API before assuming the add-on itself is broken.
