# pylint: disable=line-too-long
"""geniClient.py
    Functions for geni REST API access and processing
    """
import os, logging, time
from flask import session
import requests
import json

BASE_URL = 'https://www.geni.com/'
REDIRECT_URL = os.getenv('GENI_REDIRECT_URL', 'http://localhost:5000/home')
AUTH_URL = 'platform/oauth/authorize'

# Rate-limit stopgap: App 2134 is new and, until Geni approves it, capped at
# the unapproved default of 1 request/10s — too slow for anything but the
# shallowest searches. App 220 is the older, already-approved GFDC app with a
# higher limit, and its Callback URL already matches this same Heroku domain,
# so no separate redirect URL is needed. Setting GENI_USE_LEGACY_APP=1 (a
# Heroku config var) switches every new login to App 220 instead of 2134.
# Flip it back to 0/unset once 2134 is approved. A token issued under one app
# cannot be reused under the other, so users must log in again after flipping
# this — it takes effect on next login, not mid-session.
USE_LEGACY_APP = os.getenv('GENI_USE_LEGACY_APP', '0').strip().lower() in ('1', 'true', 'yes')
if USE_LEGACY_APP:
    CLIENT_ID = os.getenv('GENI_CLIENT_ID_LEGACY', '')
    CLIENT_SECRET = os.getenv('GENI_CLIENT_SECRET_LEGACY', '')
else:
    CLIENT_ID = os.getenv('GENI_CLIENT_ID', '')
    CLIENT_SECRET = os.getenv('GENI_CLIENT_SECRET', '')
TOKEN_URL = 'https://www.geni.com/platform/oauth/request_token'
PROF_URL = 'https://www.geni.com/api/profile/immediate-family?fields=id,deleted,merged_into,name,guid'
IMM_FAM_URL = 'https://www.geni.com/api/?/immediate-family?fields=id,deleted,merged_into,name,guid'
INVALIDATE_URL = 'https://www.geni.com/platform/oauth/invalidate_token'
PUBLIC_URL = 'http://www.geni.com/people/private/{guid}'
OTHERS_URL = 'https://www.geni.com/api/profile-G{guid}'
GENI_API_SLEEP_REMAINING = 50
GENI_API_SLEEP_LIMIT = 50
GENI_API_SLEEP_WINDOW = 10
_RATE_LOGGED = None  # last (limit, window) logged, so we log once per change

LOGGER = logging.getLogger()
logging.getLogger("requests").setLevel(logging.WARNING)

def build_auth_url():
    """Create the OAuth url for the application"""
    LOGGER.debug("buildAuthUrl")
    LOGGER.info('build_auth_url: using %s Geni app (client_id starts %s...)',
                'LEGACY (220)' if USE_LEGACY_APP else 'primary (2134)',
                CLIENT_ID[:8] if CLIENT_ID else '<empty>')
    params = {
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT_URL
    }
    params = '&'.join(['%s=%s' % (k, v) for k, v in params.items()])
    url = '%s%s?%s' % (BASE_URL, AUTH_URL, params)
    return url

def get_new_token(code):
    """Get the authorization tokens from OAuth"""
    LOGGER.debug("get_new_token")

    params = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'redirect_url': REDIRECT_URL
    }

    token_response = requests.get(TOKEN_URL, params=params)
    token_response = token_response.text
    return token_response

def get_refreshed_token(refresh_token):
    """Refresh an expired token via OAuth"""
    LOGGER.debug("get_refreshed_token")

    params = {
        'client_id': CLIENT_ID,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }
    token_response = requests.get(TOKEN_URL, params=params)
    token_response = token_response.text
    LOGGER.info('get_refreshed_token returns %s', token_response)
    return token_response

def get_profile_details(access_token, refresh_token, profile_id, current_step):
    """Get the profile details for a given profile ID"""
    global GENI_API_SLEEP_REMAINING, GENI_API_SLEEP_WINDOW, GENI_API_SLEEP_LIMIT
    LOGGER.debug("get_profile_details - id:%s step:%s", profile_id, str(current_step))
    payload = {'access_token':access_token}
    if 0 == GENI_API_SLEEP_REMAINING:
        LOGGER.debug('sleeping before geni api calling')
        time.sleep(GENI_API_SLEEP_WINDOW)
        GENI_API_SLEEP_REMAINING = GENI_API_SLEEP_LIMIT

    continue_flag = True
    profile_object = None
    new_access_token = None
    new_refresh_token = None
    api_error_attempts = 0
    max_api_error_attempts = 5  # bounded retry so a genuinely denied profile
                                # still fails eventually instead of looping forever
    while continue_flag:
        try:
            if not profile_id:
                profile_response = requests.get(PROF_URL, params=payload)
            else:
                url = IMM_FAM_URL.replace('?', profile_id, 1)
                profile_response = requests.get(url, params=payload)
            LOGGER.debug("Header X-API-Rate-Limit: %s", profile_response.headers.get('X-API-Rate-Limit'))
            LOGGER.debug("Header X-API-Rate-Remaining: %s", profile_response.headers.get('X-API-Rate-Remaining'))
            LOGGER.debug("Header X-API-Rate-Window: %s", profile_response.headers.get('X-API-Rate-Window'))
            lim = profile_response.headers.get('X-API-Rate-Limit')
            win = profile_response.headers.get('X-API-Rate-Window')
            rem = profile_response.headers.get('X-API-Rate-Remaining')
            if lim is not None:
                GENI_API_SLEEP_LIMIT = int(lim)
            if win is not None:
                GENI_API_SLEEP_WINDOW = int(win)
            if rem is not None:
                GENI_API_SLEEP_REMAINING = int(rem)
            global _RATE_LOGGED
            if _RATE_LOGGED != (GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW):
                _RATE_LOGGED = (GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW)
                LOGGER.info('Geni API rate limit: %d requests per %ds window',
                            GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW)
            profile_object = get_profile_obj(profile_response.text)
            # Anything other than a clean SUCCESS here — a 200 whose body is a
            # Geni API error (not an OAuth error), or a body that didn't parse
            # as JSON at all — looks identical, from here, to a real "profile
            # access denied". But on a throttled app it is often actually a
            # rate-limit rejection (Geni doesn't always answer throttling with
            # a plain HTTP 429). Retry a bounded number of times before
            # accepting it as a genuine denial, instead of failing instantly.
            if profile_object.get('status') != 'SUCCESS' \
                    and api_error_attempts < max_api_error_attempts:
                api_error_attempts += 1
                LOGGER.warning('get_profile_details non-success response for '
                                'profile %s (status=%r, attempt %d/%d) - '
                                'retrying in %ds in case this is rate-limit '
                                'throttling rather than a real denial',
                                profile_id, profile_object.get('status'),
                                api_error_attempts, max_api_error_attempts,
                                GENI_API_SLEEP_WINDOW)
                time.sleep(GENI_API_SLEEP_WINDOW)
                continue
            continue_flag = False
        except GeniOAuthError as goae:
            LOGGER.error('Geni oauth error - %s', goae)
            token_text = get_refreshed_token(refresh_token)
            LOGGER.debug('get_refreshed_token returned: %s', token_text)
            token_response = json.loads(token_text)
            access_token = new_access_token = token_response['access_token']
            refresh_token = new_refresh_token = token_response['refresh_token']
            payload = {'access_token':new_access_token}
        except:     #Catch all errors
            LOGGER.exception('Geni api connection error...retrying: ')
            time.sleep(5)
    #print profile_response.text

    profile_object['access_token'] = new_access_token if new_access_token != None else access_token
    profile_object['refresh_token'] = new_refresh_token if new_refresh_token != None else refresh_token
    return profile_object

def get_other_profile(access_token, guid):
    """Retrieve the profile of the non-logged in user as specified.

    Unlike get_profile_details, this used to make a single bare request with
    no rate-limit awareness or retry — on a throttled app (e.g. a newly
    registered, unapproved app at Geni's default 1 request/10s) a 429 here
    surfaced to users as the generic, misleading "This profile access is
    denied" message. It now shares the same rate-limit globals as
    get_profile_details and retries transient throttling instead of giving
    up on the first request."""
    global GENI_API_SLEEP_REMAINING, GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW
    LOGGER.debug("get_other_profile")
    payload = {'access_token':access_token}
    url = OTHERS_URL.replace('{guid}', guid)

    if GENI_API_SLEEP_REMAINING == 0:
        LOGGER.debug('get_other_profile sleeping before geni api calling')
        time.sleep(GENI_API_SLEEP_WINDOW)
        GENI_API_SLEEP_REMAINING = GENI_API_SLEEP_LIMIT

    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        try:
            profile_response = requests.get(url, params=payload, timeout=30)
        except requests.RequestException as e:
            LOGGER.warning('get_other_profile network error (attempt %d/%d): %s',
                            attempt, max_attempts, e)
            time.sleep(5)
            continue

        lim = profile_response.headers.get('X-API-Rate-Limit')
        win = profile_response.headers.get('X-API-Rate-Window')
        rem = profile_response.headers.get('X-API-Rate-Remaining')
        if lim is not None and win is not None:
            GENI_API_SLEEP_LIMIT = int(lim)
            GENI_API_SLEEP_WINDOW = int(win)
        if rem is not None:
            GENI_API_SLEEP_REMAINING = int(rem)
            global _RATE_LOGGED
            if _RATE_LOGGED != (GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW):
                _RATE_LOGGED = (GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW)
                LOGGER.info('Geni API rate limit: %d requests per %ds window',
                            GENI_API_SLEEP_LIMIT, GENI_API_SLEEP_WINDOW)

        if profile_response.status_code == 429:
            wait = int(profile_response.headers.get('Retry-After', GENI_API_SLEEP_WINDOW))
            LOGGER.warning('get_other_profile rate limited (429); sleeping %ss '
                            '(attempt %d/%d)', wait, attempt, max_attempts)
            time.sleep(wait)
            continue

        if profile_response.status_code >= 500:
            LOGGER.warning('get_other_profile Geni answered %s; retrying in 10s '
                            '(attempt %d/%d)', profile_response.status_code,
                            attempt, max_attempts)
            time.sleep(10)
            continue

        return profile_response.text

    LOGGER.error('get_other_profile giving up after %d attempts (still throttled '
                 'or erroring)', max_attempts)
    return profile_response.text

def get_profile_obj(profile_response):
    """Parse the JSON profile response and build return object"""
    LOGGER.debug("get_profile_obj")
    data = {}
    try:
        jsoncontents = json.loads(profile_response)
    except ValueError:
        LOGGER.error("get_profile_obj error decoding JSON: %s", profile_response)
        return data
    error = jsoncontents.get('error', False)
    if error and jsoncontents['error']['type'] == 'OAuthException':
        raise GeniOAuthError(jsoncontents['error']['message'])
    elif error != False:
        data['status'] = 'API_ERROR'
        return data
    data['status'] = 'SUCCESS'

    public_url = PUBLIC_URL
    public_url = public_url.replace('{guid}', jsoncontents['focus']['guid'])
    data['id'] = jsoncontents['focus']['id']
    data['profileName'] = jsoncontents['focus'].get('name', '')
    data['geniLink'] = public_url
    data['guid'] = jsoncontents['focus']['guid']
    contents = jsoncontents['nodes']
    relations = []
    for node in contents:
        if node.startswith('profile') and jsoncontents['focus']['id'] != contents[node]['id']:
            # Discard deleted and merged_into here
            relation = contents[node]
            delete_flag = relation.get('deleted', 0)
            merge_flag = relation.get('merged_into', 0)
            if delete_flag == False and merge_flag == 0:
                try:
                    relations.append({'id':contents[node]['id']})
                except:
                    pass
    data['relations'] = relations
    LOGGER.debug("get_profile_obj details - profileName=%s, guid=%s, relations count=%d", data['profileName'], data['guid'], len(relations))
    return data

SEARCH_URL = 'https://www.geni.com/api/profile/search'

def search_profiles(access_token, names, page='1'):
    """Search Geni profiles by name (GET /api/profile/search?names=...).
    Returns {'results': [{'guid','name','link'}], 'page': n, 'has_next': bool}."""
    LOGGER.debug("search_profiles names=%s page=%s", names, page)
    payload = {'access_token': access_token, 'names': names, 'page': page}
    resp = requests.get(SEARCH_URL, params=payload)
    data = {'results': [], 'page': 1, 'has_next': False}
    try:
        contents = json.loads(resp.text)
    except ValueError:
        LOGGER.error("search_profiles bad JSON: %s", resp.text[:200])
        return data
    if isinstance(contents, dict) and contents.get('error'):
        LOGGER.error("search_profiles API error: %s", contents['error'])
        return data
    data['page'] = contents.get('page', 1)
    data['has_next'] = bool(contents.get('next_page'))
    for prof in contents.get('results', []):
        guid = str(prof.get('guid', '') or '')
        if not guid:
            # fall back: extract digits from the api id (e.g. 'profile-123')
            pid = str(prof.get('id', '') or '')
            guid = ''.join(ch for ch in pid if ch.isdigit())
        if not guid:
            continue
        data['results'].append({
            'guid': guid,
            'name': prof.get('name', '(unnamed profile)'),
            'link': PUBLIC_URL.replace('{guid}', guid)
        })
    return data

def invalidate_token(access_token):
    """Invalidate the given access token via the API for logging out"""
    LOGGER.debug("invalidateToken")
    payload = {'access_token':access_token}
    requests.get(INVALIDATE_URL, params=payload)

class GeniOAuthError(Exception):
    """Custom exception raised when session expires and we need to renew"""
    def __init__(self, value):
        super(GeniOAuthError, self).__init__(value)
        self.value = value
    def __str__(self):
        return repr(self.value)

