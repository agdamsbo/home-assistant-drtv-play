#
#  This code comes from the Kodi drnu addon.
#  https://github.com/xbmc-danish-addons/plugin.video.drnu
#
#  The login flow (full_login/oidc_token/refresh_token/exchange_token) is
#  ported from that addon's current tvapi.py so that a DRTV account can be
#  used to browse/sync "My List" and "Continue watching", not just
#  anonymous catalogue browsing.
#

import base64
import hashlib
import logging
import pickle
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import requests_cache
from dateutil import parser

_LOGGER = logging.getLogger(__name__)


CHANNEL_IDS = [20875, 20876, 192099, 192100, 20892]
CHANNEL_PRESET = {
    'DR1': 1,
    'DR2': 2,
    'DR Ramasjang': 3,
    'DRTV': 4,
    'DRTV Ekstra': 5
}
URL = 'https://production.dr-massive.com/api'
LOGIN_GRAPHQL_URL = 'https://login.dr.dk/graphql'
LOGIN_AUTHORIZE_URL = 'https://login.dr.dk/oidc/authorize'
LOGIN_TOKEN_URL = 'https://login.dr.dk/oidc/token'
CLIENT_ID = '283ba39a2cf31d3b81e922b8'
REDIRECT_URI = 'https://www.dr.dk/drtv/callback'
GET_TIMEOUT = 10
CACHEPATH = Path(__file__).parent/'cache'
CACHEPATH.mkdir(exist_ok=True, parents=True)
EXPIRE_HOURS = 24
CLEAUP_EVERY = 7
TOKEN_REFRESH_MARGIN = timedelta(hours=1)


class ApiException(Exception):
    # normally pass this exception, to keep API alive in production
    pass


class ApiAuthException(ApiException):
    """Raised when a login (initial or refresh) fails - credentials are bad."""


def _find_image_url(obj):
    """Recursively search for something that looks like an image URL.

    Used as a last resort by pick_thumbnail() below, in case a given DR
    endpoint doesn't use the flat {'tile': 'https://...'} shape we expect
    (untested against the live API - see pick_thumbnail's docstring).
    """
    if isinstance(obj, str):
        return obj if obj.startswith('http') else None
    if isinstance(obj, dict):
        for value in obj.values():
            found = _find_image_url(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_image_url(value)
            if found:
                return found
    return None


def pick_thumbnail(images, preferred=('tile', 'poster', 'square', 'logo')):
    """Best-effort thumbnail URL lookup from a DR "images" object.

    DR doesn't necessarily expose the same label on every item/endpoint
    (episodes vs. channels vs. category pages), and the ported code here
    hasn't been verified against the live API for every shape - so this
    checks a handful of known flat labels first and, if none match, falls
    back to a recursive search for anything that looks like an image URL
    rather than silently showing nothing. If thumbnails are still missing
    after this, enable debug logging for this integration and check for
    the "no recognizable image URL" message below to see the actual shape
    DR is returning, so the label list here can be corrected.
    """
    if not images:
        return None
    for label in preferred:
        value = images.get(label)
        if isinstance(value, str) and value:
            return value
    found = _find_image_url(images)
    if found is None:
        _LOGGER.debug(
            "DRTV: no recognizable image URL in images object with keys %s: %r",
            list(images.keys()), images,
        )
    return found


# ---------------------------------------------------------------------------
# Login helpers. These are plain functions (no Api instance needed) since
# they're also used by the config flow to validate credentials before an
# Api object - and its on-disk token cache - is created.
# ---------------------------------------------------------------------------

def _generate_code_verifier(length: int = 64) -> str:
    return secrets.token_urlsafe(length)[:length]


def _generate_code_challenge(code_verifier: str) -> str:
    sha256 = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256).decode().rstrip('=')


# The transaction fragment/queries below are DR's login.dr.dk GraphQL
# contract. They're copied verbatim from the working Kodi add-on rather
# than trimmed, since this is a strict schema and DR does not publish
# stable documentation for it.
_TRANSACTION_FRAGMENT = (
    "fragment useTransactionTransactionFragment on Transaction { "
    "... on AuthenticatedAuthenticationTransaction { id email registration href __typename } "
    "... on UnauthenticatedAuthenticationTransaction { id email __typename } "
    "... on UnverifiedAuthenticationTransaction { id email name __typename } "
    "... on UnrecognizedAuthenticationTransaction { id email statisticsConsentDefinition "
    "{ id type version locale permissions headline summary body __typename } "
    "preferencesConsentDefinition { id type version locale permissions headline summary body __typename } "
    "newsletterConsentDefinition { id type version locale permissions headline summary body __typename } "
    "__typename } "
    "... on UnidentifiedAuthenticationTransaction { id __typename } "
    "... on CompletedEmailVerificationTransaction { id emailVerificationVariant: variant email __typename } "
    "... on PendingEmailVerificationTransaction { id emailVerificationVariant: variant email __typename } "
    "... on CompletedPasswordChangeTransaction { id passwordChangeVariant: variant __typename } "
    "... on PendingPasswordChangeTransaction { id passwordChangeVariant: variant __typename } "
    "... on PendingDeletionConfirmationTransaction { id __typename } "
    "... on CompletedDeletionConfirmationTransaction { id __typename } "
    "... on SettingsTransaction { id identity { id email name roles __typename } "
    "statisticsConsentDefinition { id type version locale permissions headline summary body __typename } "
    "preferencesConsentDefinition { id type version locale permissions headline summary body __typename } "
    "newsletterConsentDefinition { id type version locale permissions headline summary body __typename } "
    "statisticsConsentRevision { id status definition createdAt __typename } "
    "preferencesConsentRevision { id status definition createdAt __typename } "
    "newsletterConsentRevision { id status definition createdAt __typename } "
    "referBackUri referBackName sessionState expiresAt __typename } "
    "... on PendingEUPTransaction { id href __typename } "
    "... on CompletedEUPTransaction { id __typename } __typename }"
)
_TRANSACTION_QUERY = (
    "query useTransactionTransactionQuery($id: ID!) { transaction(id: $id) { "
    "... on Node { id __typename } ...useTransactionTransactionFragment __typename } }"
    + _TRANSACTION_FRAGMENT
)
_IDENTIFY_QUERY = (
    "mutation useTransactionIdentificationMutation($input: IdentificationInput!) { "
    "identify(input: $input) { ... on Node { id __typename } ... on Error { code message __typename } "
    "...useTransactionTransactionFragment __typename } }"
    + _TRANSACTION_FRAGMENT
)
_AUTHENTICATE_QUERY = (
    "mutation useTransactionAuthenticationMutation($input: AuthenticationInput!) { "
    "authenticate(input: $input) { ... on Node { id __typename } ... on Error { code message __typename } "
    "...useTransactionTransactionFragment __typename } }"
    + _TRANSACTION_FRAGMENT
)


def _oidc_token(data: dict) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    res = requests.post(LOGIN_TOKEN_URL, data=data, headers=headers, timeout=GET_TIMEOUT)
    if res.status_code != 200:
        return {'status_code': res.status_code, 'error': res.text}
    return res.json()


def refresh_token(refresh_token_value: str) -> dict:
    data = {"client_id": CLIENT_ID, "refresh_token": refresh_token_value, "grant_type": "refresh_token"}
    return _oidc_token(data)


def exchange_token(tokens: dict) -> dict:
    data = {
        "accessToken": tokens['access_token'], "identityToken": tokens['id_token'],
        "scopes": ["Catalog"], "device": "web_browser", "optout": False,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    res = requests.post(URL + '/authorization/exchange', json=data, headers=headers, timeout=GET_TIMEOUT)
    if res.status_code != 200:
        return {'status_code': res.status_code, 'error': res.text}
    return res.json()


def full_login(email: str, password: str) -> dict:
    """Log in with a DRTV email/password and return OIDC access tokens.

    This is a headless port of DR's web login flow: it drives the same
    login.dr.dk "transaction" GraphQL API the https://dr.dk/drtv web
    frontend uses, rather than opening a browser. On success this
    returns a dict with access_token/id_token/refresh_token (suitable
    for exchange_token()); on failure it returns {'error': ...}.
    """
    session = requests.Session()

    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    params = {
        "client_id": CLIENT_ID,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "state": f'{{"code_verifier":"{code_verifier}","logonRedirectPath":"/","optout":false}}',
        "response_type": "code",
        "scope": "openid roles tracking profile email offline_access",
    }
    res = session.get(LOGIN_AUTHORIZE_URL, params=params, timeout=GET_TIMEOUT)
    if res.status_code != 200:
        return {'status_code': res.status_code, 'error': res.text}

    trans = urlparse(res.url).path.split('/')[-1]
    headers = {'content-type': 'application/json'}

    trans_data = {
        "operationName": "useTransactionTransactionQuery",
        "variables": {"id": trans}, "query": _TRANSACTION_QUERY,
    }
    identify_data = {
        "operationName": "useTransactionIdentificationMutation",
        "variables": {"input": {"transaction": trans, "email": email}}, "query": _IDENTIFY_QUERY,
    }
    authenticate_data = {
        "operationName": "useTransactionAuthenticationMutation",
        "variables": {"input": {"transaction": trans, "password": password}}, "query": _AUTHENTICATE_QUERY,
    }

    session.post(LOGIN_GRAPHQL_URL, json=trans_data, headers=headers, timeout=GET_TIMEOUT)
    session.post(LOGIN_GRAPHQL_URL, json=identify_data, headers=headers, timeout=GET_TIMEOUT)
    auth_res = session.post(LOGIN_GRAPHQL_URL, json=authenticate_data, headers=headers, timeout=GET_TIMEOUT)

    auth_json = auth_res.json()
    authenticate = auth_json.get('data', {}).get('authenticate') or {}
    if 'errors' in auth_json or authenticate.get('__typename') == 'Error':
        message = authenticate.get('message') or auth_json.get('errors', [{}])[0].get('message', 'login failed')
        return {'error': message}
    href = authenticate.get('href')
    if not href:
        # e.g. wrong password -> UnauthenticatedAuthenticationTransaction with no href
        return {'error': 'invalid_credentials'}

    res2 = session.get(href, timeout=GET_TIMEOUT)
    if res2.status_code != 200:
        return {'status_code': res2.status_code, 'error': res2.text}
    codes = parse_qs(urlparse(res2.url).query).get('code')
    if not codes:
        return {'error': 'invalid_credentials'}

    data = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
        "code": codes[0],
        "grant_type": "authorization_code",
    }
    return _oidc_token(data)


def anonymous_tokens() -> dict:
    data = {"deviceId": _deviceid(), "scopes": ["Catalog"], "optout": False}
    params = {'device': 'web_browser', 'ff': 'idp,ldp,rpt', 'lang': 'da', 'supportFallbackToken': True}
    res = requests.post(URL + '/authorization/anonymous-sso?', json=data, params=params, timeout=GET_TIMEOUT)
    if res.status_code != 200:
        return {'status_code': res.status_code, 'error': res.text}
    return res.json()


def _deviceid() -> str:
    v = int(Path(__file__).stat().st_mtime)
    h = hashlib.md5(str(v).encode('utf-8')).hexdigest()
    return '-'.join([h[:8], h[8:12], h[12:16], h[16:20], h[20:32]])


class Api():
    def __init__(self, username=None, password=None, cache_path=None,
                 caching=True, expire_hours=EXPIRE_HOURS, cleanup_every=CLEAUP_EVERY):
        self.username = username or None
        self.password = password or None
        self.cachePath = cache_path or CACHEPATH
        self.cachePath.mkdir(exist_ok=True, parents=True)
        self.expire_hours = expire_hours
        self.cleanup_every = cleanup_every
        self.caching = caching
        self.expire_seconds = 3600*self.expire_hours if self.expire_hours >= 0 else self.expire_hours
        self.access_tokens = {}
        self._user_name = ''
        self._user_token = None
        self._profile_token = None
        self._token_expire = None
        self.init_sqlite_db()

        # Anonymous and logged-in sessions are cached separately so
        # switching accounts (or logging out) doesn't reuse stale tokens.
        slug = 'anon' if not self.username else hashlib.md5(self.username.encode('utf-8')).hexdigest()[:12]
        self.token_file = Path(f'{self.cachePath}/token_{slug}.p')

        self.refresh_tokens()

    def init_sqlite_db(self):
        if not (self.cachePath/'requests_cleaned').exists():
            if (self.cachePath/'requests.cache.sqlite').exists():
                (self.cachePath/'requests.cache.sqlite').unlink()
        request_fname = str(self.cachePath/'requests.cache')
        self.session = requests_cache.CachedSession(
            request_fname, backend='sqlite', expire_after=self.expire_seconds)

        if (self.cachePath/'requests_cleaned').exists():
            if (time.time() - (self.cachePath/'requests_cleaned').stat().st_mtime)/3600/24 < self.cleanup_every:
                # less than self.cleanup_every days since last cleaning, no need...
                return

        # doing recache.db cleanup
        try:
            self.session.remove_expired_responses()
        except Exception:
            if (self.cachePath/'requests.cache.sqlite').exists():
                (self.cachePath/'requests.cache.sqlite').unlink()
            self.session = requests_cache.CachedSession(
                request_fname, backend='sqlite', expire_after=self.expire_seconds)
        (self.cachePath/'requests_cleaned').write_text(str(datetime.now()))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self.username)

    @property
    def user_name(self) -> str:
        if not self._user_name:
            if not self.logged_in:
                self._user_name = 'anonymous'
            else:
                try:
                    self._user_name = self.get_profile().get('name', self.username)
                except ApiException:
                    self._user_name = self.username
        return self._user_name

    def read_tokens(self, tokens):
        if 'value' in tokens[0]:
            # anonymous flow
            time_str = tokens[0]['expirationDate'].split('.')[0]
            self._user_token = tokens[0]['value']
            self._profile_token = tokens[1]['value']
            self._user_name = 'anonymous'
        else:
            # oidc/logged-in flow
            time_str = tokens[0]['Expires'].split('.')[0]
            self._user_token = tokens[0]['Token']
            self._profile_token = tokens[1]['Token']
            self._user_name = ''

        try:
            self._token_expire = datetime.strptime(time_str + 'Z', '%Y-%m-%dT%H:%M:%S%z')
        except ValueError:
            time_struct = time.strptime(time_str, '%Y-%m-%dT%H:%M:%S')
            self._token_expire = datetime(*time_struct[0:6], tzinfo=timezone.utc)

    def request_tokens(self):
        """(Re)authenticate from scratch. Returns an error string, or None on success."""
        self._user_token = None
        self._profile_token = None

        if self.logged_in:
            access_tokens = full_login(self.username, self.password)
            if 'error' in access_tokens:
                return access_tokens['error']
            self.access_tokens = access_tokens
            tokens = exchange_token(access_tokens)
        else:
            self.access_tokens = {}
            tokens = anonymous_tokens()

        if 'error' in tokens:
            return tokens['error']

        self.read_tokens(tokens)
        with self.token_file.open('wb') as fh:
            pickle.dump([tokens, self.access_tokens], fh)
        return None

    def refresh_tokens(self):
        if self._user_token is None and self.token_file.exists():
            try:
                with self.token_file.open('rb') as fh:
                    loaded = pickle.load(fh)
                if isinstance(loaded, list) and len(loaded) == 2:
                    tokens, self.access_tokens = loaded
                    self.read_tokens(tokens)
            except Exception:  # noqa: BLE001 - corrupt/old cache file, just re-authenticate
                self._user_token = None

        if self._user_token is None:
            err = self.request_tokens()
            if err:
                raise ApiAuthException(f'DRTV login failed: {err}')
            return

        if self._token_expire and (self._token_expire - datetime.now(timezone.utc)) < TOKEN_REFRESH_MARGIN:
            failed_refresh = not (self.logged_in and 'refresh_token' in self.access_tokens)
            tokens = None
            if not failed_refresh:
                access_tokens = refresh_token(self.access_tokens['refresh_token'])
                if 'error' in access_tokens:
                    failed_refresh = True
                    self.access_tokens = {}
                else:
                    tokens = exchange_token(access_tokens)
                    if 'error' in tokens:
                        failed_refresh = True
                    else:
                        self.access_tokens = access_tokens

            if failed_refresh:
                err = self.request_tokens()
                if err:
                    raise ApiAuthException(f'DRTV login failed: {err}')
            else:
                self.read_tokens(tokens)
                with self.token_file.open('wb') as fh:
                    pickle.dump([tokens, self.access_tokens], fh)

    def user_token(self):
        self.refresh_tokens()
        return self._user_token

    def profile_token(self):
        self.refresh_tokens()
        return self._profile_token

    # ------------------------------------------------------------------
    # Generic request helper
    # ------------------------------------------------------------------

    def _request_get(self, url, params=None, headers=None, use_cache=True):
        if use_cache and self.caching:
            u = self.session.get(url, params=params, headers=headers, timeout=GET_TIMEOUT)
        else:
            u = requests.get(url, params=params, headers=headers, timeout=GET_TIMEOUT)
        if u.status_code == 200:
            return u.json()
        raise ApiException(u.text)

    def _auth_headers(self):
        return {"X-Authorization": f'Bearer {self.profile_token()}'}

    # ------------------------------------------------------------------
    # Browsing
    # ------------------------------------------------------------------

    def get_programcard(self, path, data=None, use_cache=True, ff='ldp,rpt'):
        url = URL + '/page?'
        if data is None:
            data = {
                'ff': ff,
                'item_detail_expand': 'all',
                'list_page_size': '24',
                'max_list_prefetch': '3',
                'path': path
            }
        else:
            data['path'] = path
        return self._request_get(url, params=data, use_cache=use_cache)

    def get_item(self, id, use_cache=True):
        url = URL + f'/items/{int(id)}?'
        return self._request_get(url, use_cache=use_cache)

    def get_next(self, path, use_cache=True, headers=None):
        url = URL + path
        return self._request_get(url, headers=headers, use_cache=use_cache)

    def get_list(self, id, param, use_cache=True):
        if isinstance(id, str):
            id = int(id.replace('ID_', ''))
        url = URL + f'/lists/{id}'
        data = {'page_size': '24'}
        if param != 'NoParam':
            data['param'] = param
        return self._request_get(url, params=data, use_cache=use_cache)

    def get_recommendations(self, id, use_cache=True):
        url = URL + f'/recommendations/{id}'
        data = {'page_size': '24'}
        return self._request_get(url, params=data, headers=self._auth_headers(), use_cache=use_cache)

    def kids_item(self, item):
        if 'classification' in item:
            if item['classification']['code'] in ['DR-Ramasjang', 'DR-Minisjang']:
                return True
        if 'categories' in item:
            for cat in ['dr minisjang', 'dr ramasjang', 'dr ultra']:
                if cat in item['categories']:
                    return True
        return False

    def unfold_list(self, item, filter_kids=False, headers=None):
        items = item['items']
        next_js = item
        while 'next' in next_js.get('paging', {}):
            next_js = self.get_next(next_js['paging']['next'], headers=headers)
            items += next_js['items']
        if filter_kids:
            items = [item for item in items if not self.kids_item(item)]
        return items

    def search(self, term):
        url = URL + '/search'
        data = {
            'item_detail_expand': 'all',
            'list_page_size': '24',
            'group': 'true',
            'term': term
        }
        return self._request_get(url, params=data, headers=self._auth_headers(), use_cache=False)

    def get_latest(self, term):
        item = {}
        try:
            id = int(term)
        except Exception:
            id = None

        if term.startswith('/'):
            path = term
        elif id:
            path = self.get_item(id)['path']
        else:
            # search
            res = self.search(term)
            path = ''
            for key, val in res.items():
                if key not in ['term', 'total', 'people']:
                    if val['size'] > 0:
                        path = res[key]['items'][0]['path']
                        break
        if path:
            card = self.get_programcard(path, ff='idp,ldp,rpt')
            if card['item']['type'] == 'episode':
                # Make sure we get the latest episode, fix error for bonderøven
                season = 0
                for season_item in card['item']['season']['show']['seasons']['items']:
                    if season_item['seasonNumber'] > season:
                        season = season_item['seasonNumber']
                        path = season_item['path']
                if season > 0:
                    card = self.get_programcard(path, ff='idp,ldp,rpt')

            if card['item']['type'] == 'season':
                # find latest
                item = card['item']['episodes']['items'][0]
                if len(card['item']['episodes']['items']) > 1:
                    label = 'AvailableFrom'
                    ts = parser.parse(item['customFields'][label])
                    for litem in card['item']['episodes']['items'][1:]:
                        lts = parser.parse(litem['customFields'][label])
                        if lts > ts:
                            item = litem
                            ts = lts

            elif card['item']['type'] == 'program':
                item = card['item']
        return item

    def get_home(self):
        data = dict(
            list_page_size=24,
            max_list_prefetch=1,
            item_detail_expand='all',
            path='/',
            segments='drtv,mt_K8q4Nz3,optedin',
        )
        js = self.get_programcard('/', data=data)
        items = [{'title': 'Programmer A-Å', 'path': '/kategorier/a-aa', 'icon': 'all.png'}]
        for item in js['entries']:
            title = item['title']
            if title not in ['Se Live TV', 'Vi tror, du kan lide']:  # TODO activate again when login works
                if title == '' and item['type'] == 'ListEntry':
                    title = item['list'].get('title', '')  # get the top spinner item
                if title.startswith('DRTV Hero'):
                    title = 'Daglige forslag'
                if title:
                    # Carry any thumbnail along too - it may sit on the
                    # entry itself or on the nested list, depending on the
                    # entry type, so check both.
                    images = item.get('images') or item.get('list', {}).get('images')
                    items.append({'title': title, 'path': item['list']['path'], 'images': images})
        return items

    def getLiveTV(self):
        channels = []
        schedules = self.get_channel_schedule_strings()
        for id in CHANNEL_IDS:
            card = self.get_programcard(f'/kanal/{id}')
            card['entries'][0]['schedule_str'] = schedules[id]
            channels += card['entries']
        return channels

    def get_children_front_items(self, channel):
        names = {
            'dr-ramasjang': '/ramasjang_a-aa',
            'dr-minisjang': '/minisjang/a-aa',
            'dr-ultra': '/ultra_a-aa',
            'dr': '/kategorier/a-aa',
        }
        name = names[channel]
        js = self.get_programcard(name)
        items = []
        for item in js['entries']:
            if item['type'] == 'ListEntry':
                items += self.unfold_list(item['list'])
        return items

    def get_stream(self, id):
        url = URL + f'/account/items/{int(id)}/videos?'
        headers = {"X-Authorization": f'Bearer {self.user_token()}'}
        data = {
            'delivery': 'stream',
            'device': 'web_browser',
            'ff': 'idp,ldp,rpt',
            'lang': 'da',
            'resolution': 'HD-1080',
            'sub': 'Anonymous'
        }
        u = self.session.get(url, params=data, headers=headers, timeout=GET_TIMEOUT)
        if u.status_code == 200:
            for stream in u.json():
                if stream['accessService'] == 'StandardVideo':
                    return stream
            return None
        raise ApiException(u.text)

    def get_livestream(self, path, with_subtitles=False):
        channel = self.get_programcard(path)['entries'][0]
        stream = {
            'subtitles': [],
            'url': self.get_channel_url(channel, with_subtitles)
        }
        return stream

    def get_channel_url(self, channel, with_subtitles=False, use_cache=False):
        """Return a working live-stream URL for a channel.

        DR publishes several delivery variants for the same channel - a
        Danish CDN one and an "Eu" one for viewers outside Denmark, each
        with/without subtitles - and not every channel exposes every
        variant. Querying the live channel's own customFields (the old
        approach) returns a fixed field that DR has in practice stopped
        keeping current, which is why live channels could fail to stream
        even though VOD playback (a different endpoint) works fine.
        Instead, ask the dedicated liveStreams endpoint what's actually
        available and pick the best match automatically, falling back
        through the other variants rather than assuming one fixed key.
        """
        id = channel['item']['id']
        url = URL + f'/channels/{id}/liveStreams?'
        headers = {"X-Authorization": f'Bearer {self.profile_token()}'}
        js = self._request_get(url, headers=headers, use_cache=use_cache)
        links = {item['type']: item['link'] for item in js if item.get('link')}

        if with_subtitles:
            preference = [
                'hlsWithSubtitlesURLEu', 'hlsWithSubtitlesURL', 'hlsURLEu', 'hlsURL',
            ]
        else:
            preference = [
                'hlsURLEu', 'hlsURL', 'hlsWithSubtitlesURLEu', 'hlsWithSubtitlesURL',
            ]

        for key in preference:
            if key in links:
                return links[key]

        if links:
            # Unknown/renamed variant name - better to play *something*
            # than nothing.
            return next(iter(links.values()))

        raise ApiException(f'DRTV did not return any live stream links for channel {id}')

    def get_info(self, item):
        title = item['title']
        if item['type'] == 'season':
            title += f" {item['seasonNumber']}"
        elif item.get('contextualTitle', None):
            cont = item['contextualTitle']
            if cont.count('.') >= 1 and cont.split('.', 1)[1].strip() not in title:
                title += f" ({item['contextualTitle']})"
        if len(item.get('shortDescription', '')) >= 255 and item.get('description', '') == '':
            item = self.get_item(item['id'])

        infoLabels = {'title': title}
        if item.get('shortDescription', '') and item['shortDescription'] != 'LinkItem':
            infoLabels['plot'] = item['shortDescription']
        if item.get('description', ''):
            infoLabels['plot'] = item['description']
        if item.get('tagline', ''):
            infoLabels['plotoutline'] = item['tagline']
        if item.get('customFields'):
            if item['customFields'].get('BroadcastTimeDK'):
                broadcast = parser.parse(item['customFields']['BroadcastTimeDK'])
                infoLabels['date'] = broadcast.strftime('%d.%m.%Y')
                infoLabels['aired'] = broadcast.strftime('%Y-%m-%d')
                infoLabels['year'] = int(broadcast.strftime('%Y'))
        if item.get('seasonNumber'):
            infoLabels['season'] = item['seasonNumber']
        if item.get('episodeNumber'):
            infoLabels['episode'] = item['episodeNumber']
        if item['type'] in ["movie", "season", "episode"]:
            infoLabels['mediatype'] = item['type']
        elif item['type'] == 'program':
            infoLabels['mediatype'] = 'tvshow'
        return title, infoLabels

    def get_schedules(self, channels=CHANNEL_IDS, date=None, hour=None, duration=6):
        url = URL + '/schedules?'
        now = datetime.now() - timedelta(hours=2)
        if date is None:
            date = now.strftime("%Y-%m-%d")
        if hour is None:
            hour = int(now.strftime("%H"))
        if duration <= 24:
            data = {
                'date': date,
                'hour': hour,
                'duration': duration,
                'channels': channels,
            }
            return self._request_get(url, params=data, use_cache=True)

        schedules = []
        for i in range(1, 8):
            iter_date = (now + timedelta(days=i-1)).strftime("%Y-%m-%d")
            if i*24 > duration:
                hours = duration % ((i-1)*24)
                if hours != 0:
                    schedules += self.get_schedules(channels=channels, date=iter_date, hour=hour, duration=hours)
                break
            else:
                schedules += self.get_schedules(channels=channels, date=iter_date, hour=hour, duration=24)
        return schedules

    def get_channel_schedule_strings(self, channels=CHANNEL_IDS):
        out = {}
        now = datetime.now(timezone.utc)
        for channel in self.get_schedules():
            id = int(channel['channelId'])
            out[id] = ''
            for item in channel['schedules']:
                if parser.parse(item['endDate']) > now and out[id].count('\n') < 5:
                    t = parser.parse(item['startDate']) + timedelta(hours=2)
                    start = t.strftime('%H:%M')
                    out[id] += f"{start} {item['item']['title']} \n"
        return out

    # ------------------------------------------------------------------
    # Account features (require a logged-in Api instance)
    # ------------------------------------------------------------------

    def get_profile(self, use_cache=False):
        url = URL + '/account/profile'
        params = {"ff": "idp,ldp,rpt", "lang": "da"}
        return self._request_get(url, params=params, headers=self._auth_headers(), use_cache=use_cache)

    def get_mylist(self, use_cache=False):
        url = URL + '/account/profile/bookmarks/list'
        data = {'page_size': '24'}
        headers = self._auth_headers()
        item = self._request_get(url, params=data, headers=headers, use_cache=use_cache)
        items = self.unfold_list(item, headers=headers)
        for entry in items:
            entry['in_mylist'] = True
        return items

    def get_continue(self, use_cache=False):
        url = URL + '/account/profile/continue-watching/list'
        data = {'page_size': '24'}
        headers = self._auth_headers()
        item = self._request_get(url, params=data, headers=headers, use_cache=use_cache)
        items = self.unfold_list(item, headers=headers)
        watched = self.get_profile().get('watched', {})
        for entry in items:
            entry['ResumeTime'] = float(watched.get(str(entry['id']), {'position': 0.0})['position'])
        return items

    def add_to_mylist(self, id):
        url = f'{URL}/account/profile/bookmarks/{id}'
        u = self.session.put(url, headers=self._auth_headers(), timeout=GET_TIMEOUT)
        if u.status_code != 200:
            raise ApiException(u.text)

    def delete_from_mylist(self, id):
        url = f'{URL}/account/profile/bookmarks/{id}'
        u = self.session.delete(url, headers=self._auth_headers(), timeout=GET_TIMEOUT)
        if u.status_code != 204:
            raise ApiException(u.text)
