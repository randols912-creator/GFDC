# pylint: disable=line-too-long
"""GFDC - Geni Forest Density Calculator
    For a given profile, calculate the number of relations n steps away
    Save and recall top profiles
    """

from setenvs import set_configs
import os, logging, logging.config
from datetime import datetime, timezone

set_configs()

from flask import Flask, redirect, request, session, url_for, jsonify, send_file
import json
from geni_client import build_auth_url, get_new_token, get_other_profile, \
    get_profile_details, invalidate_token, search_profiles, fetch_claimed
from flask_session import Session
from db import \
    get_top_profiles, get_top10_profiles, save_geni_profile, get_top50_profiles, setup_db, \
    save_service_token
from mail import sendEmail, send_contact_email
from rq import Queue
from worker import CONN, get_redis_url
import gfdc_refresh

APP = Flask(__name__)
LOGGER = logging.getLogger(__name__)
HOST = None
PORT = None
Q = Queue(connection=CONN)

# Make sure geni_profiles.claimed (and geni_oauth) exist before anything
# tries to query them -- otherwise the very first /top50 request after a
# fresh deploy would hit "Unknown column 'claimed'" until some other entry
# point (a login, or the next scheduled refresh_top50.py run) happened to
# call this first. Each ALTER is independently try/except'd inside
# ensure_schema(), so a momentarily-unreachable DB at boot just logs a
# warning here rather than crashing app startup.
try:
    gfdc_refresh.ensure_schema()
except Exception:
    LOGGER.exception('ensure_schema at startup failed (will retry on next '
                     'login or scheduled run)')

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

# The label under which the unattended Top 20 rescan's OAuth tokens are
# stored (db.ServiceAuth) -- just our own reference for which Geni login
# this is, never sent to Geni. Keep in sync with rescan_top20.py.
SERVICE_AUTH_LABEL = 'rschoenberg@jewishgen.org'

@APP.route('/service-login')
def service_login():
    """Same OAuth flow as /login, but flags the resulting tokens (in /home)
    to also be persisted to the DB for rescan_top20.py's unattended use --
    a normal browsing session never touches that table."""
    LOGGER.debug("service_login")
    session['seed_service_token'] = SERVICE_AUTH_LABEL
    return redirect(build_auth_url())

@APP.route('/home')
def home():
    """Handle the redirected OAuth session and capture tokens"""
    LOGGER.debug("home")
    code = request.args.get('code')
    token_response = get_new_token(code)
    try:
        set_tokens(token_response)
    except (KeyError, ValueError):
        # expired or reused OAuth code (stale tab, back button) - just log in again
        LOGGER.warning('home: invalid/expired OAuth code, redirecting to login')
        return redirect(url_for('login'))
    LOGGER.info('home just saved into session access token: %s', session['access_token'])
    # Persist the pair so the scheduled Top 50 refresh has credentials: a
    # Scheduler dyno has no session, and a queued job is authenticated with
    # whatever tokens were captured here. Logging in IS the seeding step.
    # Wrapped so a storage problem can never break a login.
    try:
        import gfdc_refresh
        gfdc_refresh.save_oauth(session['access_token'], session['refresh_token'])
    except Exception as oauth_store_err:
        LOGGER.warning('could not store tokens for the scheduled refresh: %s',
                       oauth_store_err)
    session['current_step'] = 0
    seed_label = session.pop('seed_service_token', None)
    if seed_label:
        save_service_token(
            seed_label, session['access_token'], session['refresh_token'],
            datetime.now(timezone.utc).isoformat())
        LOGGER.info('home: seeded service token for %s', seed_label)
        return ('Service login saved for automated Top 20 rescans '
                '(label: %s). You can close this tab.' % seed_label)
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
        claimed_checked = False
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
                    if not claimed_checked:
                        # Once per job, not once per step -- claimed-ness is
                        # a property of the person, doesn't change as the
                        # walk goes deeper.
                        claimed_checked = True
                        claim = fetch_claimed(session['access_token'], local_session['guid'])
                        if claim is not None:
                            gfdc_refresh.stamp_claimed(local_session['guid'], claim)
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
    """Long-running job for 4+ step requests (guest calculations from the web
    UI, and the scheduled Top 50 refresh).

    October 2026 changes, all aimed at walks that never finished:
      * One loop for both the "my profile" and "other profile" cases (they
        were two copies of the same code).
      * Scheduled-refresh jobs (params['persist_tokens']) save every rotated
        Geni token pair back to geni_oauth, so the next run starts from a
        pair Geni still honours, and checkpoint the walk to Redis
        (walk_checkpoint) so a dyno restart or a failure resumes where it
        stopped instead of starting again at step 1.
      * Any failure emails the job's recipient with the reason, instead of
        the walk simply going quiet.
    Guest jobs keep their old behaviour: their tokens are theirs, never
    stored, and nothing re-enqueues them, so they are not checkpointed.
    """
    global LOGGER
    if LOGGER == None:
        LOGGER = logging.getLogger()
    import walk_checkpoint
    LOGGER.debug("create_background_job")
    data = {}
    local_session = {}
    steps = []
    visited_set = set()
    other_id = params.get('other_id', '') or None
    step_count = int(params['step_count'])
    include_in_top50 = params['includeInTop50']
    persist = bool(params.get('persist_tokens'))
    use_ckpt = persist and bool(other_id)
    tokens = {'access': params['access_token'], 'refresh': params['refresh_token'],
              'saved': (params['access_token'], params['refresh_token'])}
    LOGGER.info('create_background_job: %s, %d steps%s', other_id or 'own profile',
                step_count, ' (scheduled refresh)' if persist else '')

    def keep_tokens(access, refresh):
        tokens['access'], tokens['refresh'] = access, refresh
        if persist and access and refresh and (access, refresh) != tokens['saved']:
            gfdc_refresh.save_oauth(access, refresh)
            tokens['saved'] = (access, refresh)

    start_step, resume = 0, None
    if use_ckpt:
        state = walk_checkpoint.load(other_id, step_count)
        meta = state['meta'] if state else {}
        if state and int(meta.get('completed_steps') or 0) >= 1 \
                and state['layer_frontier'] is not None:
            for k in ('loginProfileId', 'stepUserLink', 'guid', 'stepProfileName',
                      'totalProfiles', 'current_step'):
                if k in meta:
                    local_session[k] = meta[k]
            local_session['layer_prev'] = state['layer_prev'] or set()
            local_session['layer_frontier'] = state['layer_frontier']
            steps = list(meta.get('steps') or [])
            start_step = int(meta['completed_steps'])
            if state['new_layer'] is not None and meta.get('pos'):
                resume = (int(meta['pos']), state['new_layer'],
                          int(meta.get('unique_count') or 0))
            LOGGER.info('RESUMING %s from checkpoint: %d of %d steps done%s',
                        meta.get('stepProfileName'), start_step, step_count,
                        (', %d profiles into step %d' % (resume[0], start_step + 1))
                        if resume else '')
        elif state:
            walk_checkpoint.clear(other_id, step_count)

    def checkpoint_meta(completed, pos=None, unique_count=None):
        meta = {k: local_session.get(k) for k in
                ('loginProfileId', 'stepUserLink', 'guid', 'stepProfileName',
                 'totalProfiles', 'current_step')}
        meta.update({'completed_steps': completed, 'steps': steps,
                     'pos': pos, 'unique_count': unique_count})
        return meta

    def make_progress(step):
        def progress(pos, new_layer, unique_count, access, refresh):
            keep_tokens(access, refresh)
            walk_checkpoint.save(other_id, step_count, {
                'meta': checkpoint_meta(step, pos, unique_count),
                'layer_prev': local_session.get('layer_prev'),
                'layer_frontier': local_session.get('layer_frontier'),
                'new_layer': new_layer})
        return progress

    step = start_step
    try:
        for step in range(start_step, step_count):
            step_data = get_step_profiles_thread(
                tokens['access'], tokens['refresh'], step, visited_set, other_id,
                local_session,
                progress=make_progress(step) if (use_ckpt and step > 0) else None,
                resume=resume if step == start_step else None)
            keep_tokens(step_data['access_token'], step_data['refresh_token'])
            if include_in_top50 == 'on':
                save_geni_profile(
                    step_data,
                    local_session['stepProfileName'],
                    local_session['guid'],
                    local_session['stepUserLink'])
            # Tokens stay out of the step records (they go into checkpoints
            # and emails).
            steps.append({'step': step_data['step'], 'profiles': step_data['profiles'],
                          'total': step_data['total']})
            LOGGER.info('Calculated %s counts for step %s: %s total',
                        local_session.get('stepProfileName'), step + 1, step_data['total'])
            if use_ckpt and step + 1 < step_count:
                walk_checkpoint.save(other_id, step_count, {
                    'meta': checkpoint_meta(step + 1),
                    'layer_prev': local_session.get('layer_prev'),
                    'layer_frontier': local_session.get('layer_frontier'),
                    'new_layer': None})

            #Send Email after each step
            data['steps'] = steps
            data['profile_id'] = other_id or local_session['loginProfileId']
            data['geniLink'] = local_session['stepUserLink']
            data['guid'] = local_session['guid']
            data['profileName'] = local_session['stepProfileName']
            data['remainingSteps'] = str(step_count - step - 1)
            try:
                sendEmail(params['email'], data)
            except Exception as mail_err:
                LOGGER.error('email send failed (job continues): %s', mail_err)
    except Exception as exc:
        LOGGER.exception('create_background_job FAILED at step %d for %s',
                         step + 1, local_session.get('stepProfileName') or other_id)
        try:
            from mail import send_alert
            send_alert(params.get('email'),
                       'GFDC walk stopped: %s' % (local_session.get('stepProfileName') or other_id),
                       'The GFDC background job for %s stopped during step %d of %d.\n\n'
                       'Reason: %s: %s\n\n%s' % (
                           local_session.get('stepProfileName') or other_id,
                           step + 1, step_count, type(exc).__name__, exc,
                           'Progress is saved; the next scheduled run resumes from it.'
                           if use_ckpt else
                           'Completed steps were saved; the rest was not counted.'))
        except Exception:
            LOGGER.exception('could not send the failure email')
        raise

    if use_ckpt:
        walk_checkpoint.clear(other_id, step_count)

    data['steps'] = steps
    data['geniLink'] = local_session['stepUserLink']
    data['guid'] = local_session['guid']
    #sendEmail(params['email'], data)

    # Once per job (not per step -- claimed-ness doesn't change as the walk
    # goes deeper), and only when this profile is actually meant to land on
    # the Top 50, since that's the only place claimed/unclaimed is shown.
    if include_in_top50 == 'on' and local_session.get('guid'):
        try:
            claim = fetch_claimed(tokens['access'], local_session['guid'])
            if claim is not None:
                gfdc_refresh.stamp_claimed(local_session['guid'], claim)
                LOGGER.info('create_background_job: %s is %s',
                            local_session['guid'], 'claimed' if claim else 'unclaimed')
        except Exception:
            LOGGER.exception('create_background_job: claimed-status check failed '
                             '(counts are still saved; a later backfill run will catch it)')

def get_step_profiles_thread(access_token, refresh_token, count, visited_set, profile_id, local_session,
                             progress=None, resume=None):
    """Get all profiles for a given step when running as a background job.

    progress: optional callback(pos, new_layer, unique_count, access_token,
      refresh_token), called every walk_checkpoint.SAVE_EVERY_SECONDS while
      a step is being expanded, so the caller can checkpoint mid-step.
    resume: optional (pos, new_layer, unique_count) from such a checkpoint;
      expansion restarts at that position of the frontier.
    When either is given the frontier is walked in sorted order, so a
    position means the same thing after a restart. Without them the walk is
    exactly as before (rescan_top20 and guest jobs)."""
    global LOGGER
    if LOGGER == None:
        LOGGER = logging.getLogger()
    LOGGER.debug("get_step_profiles_thread count=%s, profile_id=%s", str(count), profile_id)
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
        if profile_data.get('status') == 'SUCCESS':
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
            # Used to fall through and die later with a bare KeyError.
            raise RuntimeError('Geni would not return the starting profile %s (status %r)'
                               % (profile_id, profile_data.get('status')))
    else:
        prev_layer = local_session['layer_prev']          # step n-2
        frontier = local_session['layer_frontier']        # step n-1 (expand these)
        new_layer = set()                                 # step n (being built)
        start = 0
        if progress is None and resume is None:
            sequence = frontier
        else:
            sequence = sorted(frontier, key=str)
            if resume is not None:
                start, new_layer, unique_count = resume[0], set(resume[1]), resume[2]
                sequence = sequence[start:]
        import time as _time
        try:
            from walk_checkpoint import SAVE_EVERY_SECONDS
        except Exception:
            SAVE_EVERY_SECONDS = 600
        last_save = _time.monotonic()
        for pos, node_key in enumerate(sequence, start):
            fetch_id = str(node_key) if isinstance(node_key, int) else node_key
            profile_data = get_profile_details(access_token, refresh_token, fetch_id, current_step)
            if profile_data.get('status') == 'SUCCESS':
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
            else:
                # Same behaviour as before (an unreadable profile contributes
                # nothing), but no longer silent.
                LOGGER.warning('step %d: skipping profile %s (status %r)',
                               current_step + 1, fetch_id, profile_data.get('status'))
                access_token = profile_data.get('access_token') or access_token
                refresh_token = profile_data.get('refresh_token') or refresh_token
            del profile_data
            if progress is not None and _time.monotonic() - last_save >= SAVE_EVERY_SECONDS:
                progress(pos + 1, new_layer, unique_count, access_token, refresh_token)
                last_save = _time.monotonic()
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
    """Handle navigation to display top 50 profiles.

    Optional ?claimed=true|false scopes the list to Geni users (claimed
    profiles) or unclaimed profiles only, each independently ranked and
    capped at 50 -- same published list as before when the param is
    omitted. A profile whose claimed status hasn't been checked yet (NULL)
    only ever shows up in the unscoped list."""
    LOGGER.debug("top50")
    data = {}
    step = request.args.get('stepValue')
    claimed_param = request.args.get('claimed')
    claimed = None
    if claimed_param is not None:
        claimed = claimed_param.strip().lower() in ('1', 'true', 'yes', 'on')
    try:
        steps = get_top50_profiles(step, claimed=claimed)
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

@APP.route('/contact', methods=['POST'])
def contact():
    """Contact form: emails the site owner via Mailgun. No login required.
    The hidden 'website' field is a honeypot - bots fill it, humans don't."""
    if (request.form.get('website') or '').strip():
        return jsonify({'sent': True})  # pretend success, send nothing
    name = (request.form.get('name') or '').strip()[:100]
    email = (request.form.get('email') or '').strip()[:200]
    message = (request.form.get('message') or '').strip()[:4000]
    if not name or not message or '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'error': 'Please fill in your name, a valid email, and a message.'}), 400
    try:
        send_contact_email(name, email, message)
        return jsonify({'sent': True})
    except Exception:
        LOGGER.exception('contact form send failed:')
        return jsonify({'error': 'Sending failed - please try again later.'}), 502

@APP.route('/searchProfiles')
def search_profiles_route():
    """Search Geni profiles by name so users can pick one instead of
    hunting for a numeric profile id. Uses the logged-in user's token."""
    if 'access_token' not in session:
        return jsonify({'error': 'not_logged_in'}), 401
    names = (request.args.get('names') or '').strip()
    page = request.args.get('page', '1')
    if not names:
        return jsonify({'results': [], 'page': 1, 'has_next': False})
    try:
        return jsonify(search_profiles(session['access_token'], names, page))
    except Exception:
        LOGGER.exception('searchProfiles error:')
        return jsonify({'error': 'search_failed'}), 502

@APP.route('/record_count', methods=['POST'])
def record_count():
    """Accept a finished density count pushed by the Geni Top 10 nightly
    crawler (geni_top10.py --density on schoenberg.com) and store it in
    geni_profiles, so the Top 50 page stays current without anyone running
    the calculator by hand. Auth: the X-Push-Key header must match the
    GENI_PUSH_KEY config var; if that var is unset, the route is disabled."""
    push_key = os.getenv('GENI_PUSH_KEY')
    if not push_key or request.headers.get('X-Push-Key') != push_key:
        return 'forbidden', 403
    try:
        guid = request.form['guid']
        name = request.form.get('name', '')
        link = request.form.get('link', '')
        steps = json.loads(request.form['steps'])
        if not guid.isdigit() or not isinstance(steps, list):
            return 'bad request', 400
        saved = 0
        for entry in steps:
            step = int(entry['step'])
            total = int(entry['total'])
            save_geni_profile({'step': step, 'total': total},
                              name, guid, link)
            saved += 1
        LOGGER.info('record_count saved %d step rows for %s (%s)',
                    saved, name, guid)
        return jsonify({'saved': saved})
    except Exception:
        LOGGER.exception('record_count error:')
        return 'error', 500

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

    app.config['SECRET_KEY'] = '12345abcde'
    # Store sessions in Redis instead of on the dyno's local disk. Filesystem
    # sessions were wiped on every deploy/restart (and don't work across multiple
    # dynos), which silently logged people out mid-visit and made a calculation
    # quietly do nothing. CONN is the app's existing Redis connection.
    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_REDIS'] = CONN
    app.config['SESSION_PERMANENT'] = False
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
