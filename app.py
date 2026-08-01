# pylint: disable=line-too-long
"""GFDC - Geni Forest Density Calculator
    For a given profile, calculate the number of relations n steps away
    Save and recall top profiles
    """

from setenvs import set_configs
import os, logging, logging.config

set_configs()

from flask import Flask, redirect, request, session, url_for, jsonify, send_file
import json
from geni_client import build_auth_url, get_new_token, get_other_profile, \
    get_profile_details, invalidate_token
from flask_session import Session
from db import \
    get_top_profiles, get_top10_profiles, save_geni_profile, get_top50_profiles, setup_db
from mail import sendEmail
from rq import Queue
from worker import CONN, get_redis_url

APP = Flask(__name__)
LOGGER = logging.getLogger(__name__)
HOST = None
PORT = None
Q = Queue(connection=CONN)

logging.config.dictConfig({
    'version': 1,              
    'disable_existing_loggers': False,  # this fixes the problem

    'formatters': {
        'standard': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        },
    },
    'handlers': {
        'default': {
            'level':'INFO',    
            'class':'logging.StreamHandler',
        },  
    },
    'loggers': {
        '': {                  
            'handlers': ['default'],        
            'level': 'INFO',  
            'propagate': True  
        }
    }
})


@APP.route('/')
def index():
    """Handle the index page"""
    LOGGER.debug("login")
    return send_file('templates/login.html')

@APP.route('/login')
def login():
    """Handle the login page"""
    LOGGER.debug("login")
    return redirect(build_auth_url())

@APP.route('/home')
def home():
    """Handle the redirected OAuth session and capture tokens"""
    LOGGER.debug("home")
    code = request.args.get('code')
    token_response = get_new_token(code)
    set_tokens(token_response)
    print('home just saved into session access token: %s', session['access_token'])
    LOGGER.info('home just saved into session access token: %s', session['access_token'])
    session['current_step'] = 0
    return send_file('templates/index.html')

def set_tokens(token_response_text):
    """Save the OAuth tokens into the session object"""
    token_response = json.loads(token_response_text)
    session['access_token'] = token_response['access_token']
    session['refresh_token'] = token_response['refresh_token']
    session['tokenExpiration'] = token_response['expires_in']
    LOGGER.info('set_tokens access_token: %s', session['access_token'])

@APP.route('/getUniqueCount')
def get_unique_count():
    """Handle unique count requests from navigation"""
    LOGGER.debug("get_unique_count")
    email = request.args.get('email')
    include_in_top50 = request.args.get('includeTop50')
    my_profile_flag = request.args.get('myProfile')
    step_count = request.args.get('stepCount')
    other_id = request.args.get('otherId')
    if my_profile_flag == 'true':
        return process_unique_count(step_count, None, include_in_top50, email)
    else:
        #Other profiles
        profile_data_text = get_other_profile(session['access_token'], other_id)
        profile_data = json.loads(profile_data_text)
        check_id = profile_data.get('id')
        if check_id == None:
            data = {}
            data['backgroundMessage'] = 'This profile access is denied.'
            return jsonify(data)
        return process_unique_count(step_count, profile_data['id'], include_in_top50, email)

def process_unique_count(step_count, profile_id, include_in_top50, email):
    """process unique count algorithm for both paths"""
    data = {}
    steps = []
    visited_set = set()
    local_session = {}
    try:
        if int(step_count) < 4:
            for step in range(0, int(step_count)):
                step_data = get_step_profiles(
                    step, visited_set, profile_id, local_session)
                check_id = step_data.get('accessError')
                if check_id == 'ACCESS_DENIED':
                    data['backgroundMessage'] = 'This profile access is denied.'
                    return jsonify(data)
                if include_in_top50 == 'on':
                    save_geni_profile(
                        step_data,
                        local_session['stepProfileName'],
                        local_session['guid'],
                        local_session['stepUserLink'])
                steps.append(step_data)
        else:
            params = {}
            params['access_token'] = session['access_token']
            params['refresh_token'] = session['refresh_token']
            params['email'] = email
            if profile_id != None:
                params['other_id'] = profile_id
            params['includeInTop50'] = include_in_top50
            params['step_count'] = step_count
            LOGGER.info('get_unique_count creating background job email %s steps %s', email, str(step_count))
            Q.enqueue_call(func='app.create_background_job', args=(params,), timeout=604800)
            data = {}
            data['backgroundMessage'] = 'Background Job started. You will receive an e-mail with the results when they are ready. The process can take several hours or more, so please be patient.'

            return jsonify(data)
    except:
        LOGGER.exception('get_unique_count error:')

    data['steps'] = steps
    if 'stepProfileName' in local_session:
        data['name'] = local_session['stepProfileName']
    return jsonify(data)


def get_step_profiles(count, visited_set, profile_id, local_session):
    """Worker method to process profiles of each member in tree"""
    global LOGGER
    if LOGGER == None:
        LOGGER = logging.getLogger()
    LOGGER.debug("get_step_profiles count=%s profile_id=%s", str(count), profile_id)
    current_step = count
    unique_count = 0
    next_step_profiles = ''
    if current_step == 0:
        profile_data = get_profile_details(session['access_token'], session['refresh_token'], profile_id, current_step)
        check_id = profile_data.get('id')
        if check_id == None:
            profile_data['accessError'] = 'ACCESS_DENIED'
            return profile_data
        if profile_data['status'] == 'SUCCESS':
            local_session['loginProfileId'] = profile_data['id']
            local_session['stepProfileName'] = profile_data['profileName']
            LOGGER.debug('get_step_profiles profile details %s', profile_data['id'])
            local_session['stepUserLink'] = profile_data['geniLink']
            local_session['guid'] = profile_data['guid']
            local_session[profile_data['id']] = profile_data
            login_profile_id = local_session['loginProfileId']
            profile_data = local_session[login_profile_id]
            session['access_token'] = profile_data['access_token']
            session['refresh_token'] = profile_data['refresh_token']
            visited_set.add(login_profile_id)
            #session['visited-' + login_profile_id] = True
            for node in profile_data['relations']:
                node_id = node['id']
                if node_id in visited_set:  # skip self / duplicate in immediate family
                    continue
                unique_count = unique_count + 1
                next_step_profiles = next_step_profiles + '*' + node['id']  # *** delimiter
                #session['visited-' + node['id']] = True
                visited_set.add(node_id)

            local_session['next_step_profiles'] = next_step_profiles[1:]
            local_session['totalProfiles'] = unique_count
    else:
        next_step_profiles = local_session['next_step_profiles']
        profile_ids = next_step_profiles.split('*')
        next_step_profiles = ''
        for profile_id in profile_ids:
            if profile_id == '' or profile_id is None:
                pass
            else:
                profile_data = local_session.get(profile_id)
                if profile_data is None:
                    profile_data = get_profile_details(session['access_token'], session['refresh_token'], profile_id, current_step)
                if profile_data['status'] == 'SUCCESS':
                    #Got profile data, process each relation
                    local_session[profile_data['id']] = profile_data
                    session['access_token'] = profile_data['access_token']
                    session['refresh_token'] = profile_data['refresh_token']
                    for node in profile_data['relations']:
                        node_id = node['id']
                        if node_id in visited_set:
                            pass
                        else:
                            next_step_profiles = next_step_profiles + '*' + node['id']
                            unique_count = unique_count + 1
                            visited_set.add(node['id'])
        local_session['next_step_profiles'] = next_step_profiles[1:]
        local_session['totalProfiles'] = local_session['totalProfiles'] + unique_count
    current_step = current_step + 1
    local_session['current_step'] = current_step
    return {'step':current_step, 'profiles':unique_count, 'total':local_session['totalProfiles']}

def create_background_job(params):
    """Builds long running job for more than 4 step requests"""
    global LOGGER
    if LOGGER == None:
        LOGGER = logging.getLogger()
    LOGGER.debug("create_background_job")
    data = {}
    local_session = {}
    steps = []
    visited_set = set()
    other_id = params.get('other_id', '')
    step_count = params['step_count']
    include_in_top50 = params['includeInTop50']
    LOGGER.debug('create_background_job step_count: %s', step_count)
    if other_id == '':
        for step in range(0, int(step_count)):
            step_data = get_step_profiles_thread(params['access_token'], params['refresh_token'], step, visited_set, None, local_session)
            params['access_token'] = step_data['access_token']
            params['refresh_token'] = step_data['refresh_token']
            if include_in_top50 == 'on':
                save_geni_profile(
                    step_data,
                    local_session['stepProfileName'],
                    local_session['guid'],
                    local_session['stepUserLink'])
            steps.append(step_data)
            LOGGER.debug('Calculated logged in profile %s counts for step %s', local_session['guid'], str(step + 1))

            #Send Email after each step
            data['steps'] = steps
            data['profile_id'] = local_session['loginProfileId']
            data['geniLink'] = local_session['stepUserLink']
            data['guid'] = local_session['guid']
            data['profileName'] = local_session['stepProfileName']
            data['remainingSteps'] = str(int(step_count) - int(step) - 1)
            try:
                sendEmail(params['email'], data)
            except Exception as mail_err:
                LOGGER.error('email send failed (job continues): %s', mail_err)
    else:
        for step in range(0, int(step_count)):
            step_data = get_step_profiles_thread(params['access_token'], params['refresh_token'], step, visited_set, params['other_id'], local_session)
            params['access_token'] = step_data['access_token']
            params['refresh_token'] = step_data['refresh_token']
            if include_in_top50 == 'on':
                save_geni_profile(
                    step_data,
                    local_session['stepProfileName'],
                    local_session['guid'],
                    local_session['stepUserLink'])
            steps.append(step_data)
            LOGGER.debug('Calculated logged in profile %s counts for step %s', local_session['guid'], str(step + 1))
            data['steps'] = steps
            data['profile_id'] = params['other_id']
            data['geniLink'] = local_session['stepUserLink']
            data['guid'] = local_session['guid']
            data['profileName'] = local_session['stepProfileName']
            data['remainingSteps'] = str(int(step_count) - int(step) - 1)
            try:
                sendEmail(params['email'], data)
            except Exception as mail_err:
                LOGGER.error('email send failed (job continues): %s', mail_err)

    data['steps'] = steps
    data['geniLink'] = local_session['stepUserLink']
    data['guid'] = local_session['guid']
    #sendEmail(params['email'], data)

def get_step_profiles_thread(access_token, refresh_token, count, visited_set, profile_id, local_session):
    """Get all profiles for a given step when running as a background job"""
    global LOGGER
    if LOGGER == None:
        LOGGER = logging.getLogger()
    LOGGER.debug("get_step_profiles_thread count=%s, access_token=%s, profile_id=%s", str(count), access_token, profile_id)
    current_step = count
    unique_count = 0
    # Memory-bounded de-duplication (rolling layers).
    # BFS theorem on an undirected graph: every edge joins two profiles whose
    # step-distance differs by at most 1. So a neighbor of a step n-1 profile is
    # only ever at step n-2, n-1, or n -- never earlier. That means we only need
    # the two previous layers (n-2, n-1) plus the new one being built (n) to
    # de-duplicate correctly; every older layer can be released. IDs are stored
    # as ints when numeric (Geni ids are), which is ~25% smaller than as strings.
    def _key(pid):
        return int(pid) if (isinstance(pid, str) and pid.isdigit()) else pid
    if current_step == 0:
        profile_data = get_profile_details(access_token, refresh_token, profile_id, current_step)
        if profile_data['status'] == 'SUCCESS':
            local_session['loginProfileId'] = profile_data['id']
            local_session['stepUserLink'] = profile_data['geniLink']
            local_session['guid'] = profile_data['guid']
            local_session['stepProfileName'] = profile_data['profileName']
            access_token = profile_data['access_token']
            refresh_token = profile_data['refresh_token']
            login_profile_id = profile_data['id']
            prev_layer = set()               # step n-2 (seeded with the source itself)
            prev_layer.add(_key(login_profile_id))
            frontier = set()                 # step n-1 (the layer we expand next)
            for node in profile_data['relations']:
                node_key = _key(node['id'])
                if node_key in prev_layer or node_key in frontier:  # skip self / duplicate
                    continue
                frontier.add(node_key)
                unique_count = unique_count + 1
            local_session['layer_prev'] = prev_layer
            local_session['layer_frontier'] = frontier
            local_session['totalProfiles'] = unique_count
    else:
        prev_layer = local_session['layer_prev']          # step n-2
        frontier = local_session['layer_frontier']        # step n-1 (expand these)
        new_layer = set()                                 # step n (being built)
        for node_key in frontier:
            fetch_id = str(node_key) if isinstance(node_key, int) else node_key
            profile_data = get_profile_details(access_token, refresh_token, fetch_id, current_step)
            if profile_data['status'] == 'SUCCESS':
                #Got profile data, process each relation - refresh tokens
                access_token = profile_data['access_token']
                refresh_token = profile_data['refresh_token']
                for node in profile_data['relations']:
                    nkey = _key(node['id'])
                    # already counted (n-2 back-edge, n-1 sibling, or found this round)?
                    if nkey in prev_layer or nkey in frontier or nkey in new_layer:
                        pass
                    else:
                        new_layer.add(nkey)
                        unique_count = unique_count + 1
            del profile_data
        # roll forward one step: n-2 <- n-1, n-1 <- n; the old n-2 is released
        local_session['layer_prev'] = frontier
        local_session['layer_frontier'] = new_layer
        local_session['totalProfiles'] = local_session['totalProfiles'] + unique_count
    current_step = current_step + 1
    local_session['current_step'] = current_step
    return {'step':current_step, 'profiles':unique_count, 'total':local_session['totalProfiles'], 'access_token':access_token, 'refresh_token':refresh_token}



@APP.route('/getProfile', methods=['GET'])
def get_profile():
    """Handle web navigation to process a profile"""
    LOGGER.debug("get_profile")
    access_token = session['access_token']
    refresh_token = session['refresh_token']
    profile_id = request.args.get('profile_id')
    profile_data = ''
    if not access_token:
        redirect(url_for('login'))
    #Load from session if already there.
    try:
        if session[profile_id] != None:
            profile_data = session[profile_id]
            return jsonify(profile_data)
    except KeyError:
        pass

    try:
        profile_data = get_profile_details(access_token, refresh_token, profile_id, 0)
        if profile_id == None:
            session['loginProfileId'] = profile_data['id']
        if profile_data != None:
            session[profile_data['id']] = profile_data
    except:
        LOGGER.exception('top50 error:')
    return jsonify(profile_data)

@APP.route('/top10')
def top10():
    """Handle navigation to display top 10 profiles"""
    LOGGER.debug("top10")
    data = {}
    try:
        steps = get_top10_profiles()
        data['top10'] = steps
    except:
        LOGGER.exception('top10 error:')
    return jsonify(data)

@APP.route('/top50')
def top50():
    """Handle navigation to display top 50 profiles"""
    LOGGER.debug("top50")
    data = {}
    step = request.args.get('stepValue')
    try:
        steps = get_top50_profiles(step)
        data['top50'] = steps
    except:
        LOGGER.exception('top50 error:')
    return jsonify(data)

@APP.route('/top')
def top():
    """Handle navigation to display top profiles"""
    LOGGER.debug("top")
    data = {}
    try:
        steps = get_top_profiles()
        data['top50'] = steps
    except:
        LOGGER.exception('top error:')
    return jsonify(data)

@APP.route('/logout')
def logout():
    """Handle navigation to logout of application"""
    access_token = session['access_token']
    invalidate_token(access_token)
    session.clear()
    return send_file('templates/login.html')

@APP.errorhandler(500)
def page_not_found(error):
    """Handle navigation for page not found error"""
    print(error)
    return 'This page does not exist', 500

def setup_app(app):
    """Do general app setup so we can run from gunicorn or command line"""
    global HOST, PORT
    try:
        setup_db()
    except Exception as _dbe:
        LOGGER.error('setup_db failed at boot (continuing without DB init): %s', _dbe)

    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['SECRET_KEY'] = '12345abcde'
    app.config['REDIS_URL'] = get_redis_url()

    # a DictStore will store everything in memory
    # this will replace the app's session handling
    app.config['SESSION_TYPE'] = 'filesystem'
    Session(app)

    PORT = int(os.environ.get('PORT', 5000))
    HOST = os.environ.get('HOST', 'localhost')
    LOGGER.info("Starting application on PORT=%d", PORT)
    # Bind to PORT if defined, otherwise default to 5000.
    app.debug = False
    #APP.testing = True
    app.secret_key = '12345abcde'
    try:
        import rq_dashboard
        app.config.from_object(rq_dashboard.default_settings)
        app.register_blueprint(rq_dashboard.blueprint, url_prefix='/rq')
    except Exception as _e:
        LOGGER.warning('rq_dashboard unavailable: %s', _e)

setup_app(APP)

if __name__ == '__main__':
    APP.run(host=HOST, port=PORT)
