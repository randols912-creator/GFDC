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

LOGGER = logging.getLogger()
logging.getLogger("requests").setLevel(logging.WARNING)

def build_auth_url():
    """Create the OAuth url for the application"""
    LOGGER.debug("buildAuthUrl")
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
    while continue_flag:
        try:
            if not profile_id:
                profile_response = requests.get(PROF_URL, params=payload)
            else:
                url = IMM_FAM_URL.replace('?', profile_id, 1)
                profile_response = requests.get(url, params=payload)
            LOGGER.debug("Header X-API-Rate-Limit: %s", profile_response.headers['X-API-Rate-Limit'])
            LOGGER.debug("Header X-API-Rate-Remaining: %s", profile_response.headers['X-API-Rate-Remaining'])
            LOGGER.debug("Header X-API-Rate-Window: %s", profile_response.headers['X-API-Rate-Window'])
            GENI_API_SLEEP_LIMIT = int(profile_response.headers['X-API-Rate-Limit'])
            GENI_API_SLEEP_REMAINING = int(profile_response.headers['X-API-Rate-Remaining'])
            GENI_API_SLEEP_WINDOW = int(profile_response.headers['X-API-Rate-Window'])
            profile_object = get_profile_obj(profile_response.text)
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
    """Retrieve the profile of the non-logged in user as specified"""
    LOGGER.debug("get_other_profile")
    payload = {'access_token':access_token}
    url = OTHERS_URL.replace('{guid}', guid)
    profile_response = requests.get(url, params=payload)
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

