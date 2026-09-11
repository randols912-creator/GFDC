#!/usr/bin/env python3
"""Heroku Scheduler entry point: run ONE not-yet-seen Top 10 list candidate
or curator through GFDC's Calculator, so it has a baseline density number
and a claimed/unclaimed status -- so it can compete for the "Highest
Density Trees" claimed/unclaimed lists (and be re-checked later) even
though it's never been run by hand or landed in the current Top 50.

Candidate pool, combined:
  1. seed/candidates_snapshot.json -- a static snapshot of profile ids
     pulled from the Geni Top 10 project's candidates.json +
     extra_candidates.json on 2026-09-11 (every non-empty list section:
     Most Complete Tree, Most Profiles Added/Managed/Curated, etc). Not
     live-synced; re-copy by hand if that project's pool changes and you
     want GFDC to pick up the new entries.
  2. The Geni Curators project's (id 9960) own collaborator roster, fetched
     live via the API and cached in the curator_roster DB table (Heroku's
     filesystem is ephemeral, so a local JSON cache like the Geni Top 10
     project uses won't survive a dyno restart). Only re-fetched when the
     cache is empty or --refresh-roster is passed -- paging through
     possibly hundreds of curators on every daily run would spend the
     shared Geni rate limit for no reason once the cache is warm.

"Already seeded" means ANY row exists for that profileId in geni_profiles,
at any step -- not just the published Top 50. One profile per run, same
public-tool-safety pattern as refresh_top50.py:

    python3 seed_candidates.py                # enqueue one
    python3 seed_candidates.py --dry-run       # show what it would pick
    python3 seed_candidates.py --force         # ignore the idle check
    python3 seed_candidates.py --refresh-roster  # re-fetch curators first

Suggested schedule: daily, on the same 'low' queue as refresh_top50.py (and
subject to the same "never block a real user" idle check) -- so add this as
a second Heroku Scheduler job, not a replacement for refresh_top50.py; they
serve different candidate pools and don't conflict with each other, just
with real users' jobs, which they're built to yield to.

Deliberately shallower than refresh_top50.py's 10 steps: this is a first
pass to get SOME number on record cheaply, not the full depth. A candidate
that later earns a spot in the actual Top 50 gets walked to full depth by
refresh_top50.py from then on.
"""
import os
import sys
import json
import logging
import argparse

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
LOGGER = logging.getLogger('seed_candidates')

import requests

# app.py builds the Redis queue and holds the job function; importing it
# also gives us the same rate-limit-aware Geni client the web path uses.
import app as gfdc_app
import gfdc_refresh
from geni_client import get_other_profile
from db import MY_DB

SEED_STEPS = os.getenv('GFDC_SEED_STEPS', '5')
NOTIFY = os.getenv('GFDC_SEED_EMAIL', '')   # empty = no email (see refresh_top50.py's
                                            # 2026-09-11 fix for why this doesn't
                                            # default to GENI_FROM_ADDR)
SEED_QUEUE = os.getenv('GFDC_SEED_QUEUE', 'low')
REQUIRE_IDLE = os.getenv('GFDC_SEED_REQUIRE_IDLE', '1') != '0'

CURATORS_PROJECT_ID = 9960
COLLABORATORS_URL = 'https://www.geni.com/api/project-{pid}/collaborators'

try:
    JOB_TIMEOUT = int(os.getenv('GFDC_SEED_JOB_TIMEOUT', str(7 * 24 * 3600)))
except ValueError:
    JOB_TIMEOUT = 7 * 24 * 3600


def _queues():
    from rq import Queue
    names = ['high', 'default', 'low']
    out = []
    for n in names:
        try:
            out.append(Queue(n, connection=gfdc_app.CONN))
        except Exception as exc:
            LOGGER.debug('could not open queue %s (%s)', n, exc)
    return out or [gfdc_app.Q]


def queue_busy():
    """Same conservative "is anything happening anywhere?" check
    refresh_top50.py uses -- see its queue_busy() for the reasoning."""
    from rq.registry import StartedJobRegistry
    pending = running = 0
    try:
        for q in _queues():
            pending += q.count
            try:
                running += StartedJobRegistry(queue=q).count
            except Exception as exc:
                LOGGER.debug('no started registry for %s (%s)', q.name, exc)
    except Exception as exc:
        LOGGER.warning('could not read the queues (%s); assuming busy', exc)
        return True
    LOGGER.info('queues (high/default/low): %d pending, %d running',
                pending, running)
    return bool(pending or running)


def load_snapshot_candidates():
    """{guid: name} from seed/candidates_snapshot.json, or {} if missing."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'seed', 'candidates_snapshot.json')
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        LOGGER.warning('could not read %s (%s)', path, exc)
        return {}
    return {str(c['id']): c.get('name', '')
            for c in data.get('candidates', []) if c.get('id')}


def fetch_curator_roster(access_token):
    """Page through the Geni Curators project's (9960) collaborators. Same
    endpoint, project id, and field set the Curator Leaderboard's own
    fetch_roster() uses (geni_leaderboard.py, Geni Top 10 project) --
    proven working there. Returns {guid: name}."""
    roster = {}
    page = 1
    url = COLLABORATORS_URL.format(pid=CURATORS_PROJECT_ID)
    while True:
        try:
            resp = requests.get(url, params={
                'access_token': access_token,
                'fields': 'id,guid,name,first_name,last_name',
                'per_page': 50, 'page': page,
            }, timeout=30)
            data = json.loads(resp.text)
        except Exception as exc:
            LOGGER.warning('fetch_curator_roster page %d failed (%s)', page, exc)
            break
        if isinstance(data, dict) and data.get('error'):
            LOGGER.warning('fetch_curator_roster API error: %s', data['error'])
            break
        results = data.get('results') or []
        if not results:
            break
        for obj in results:
            guid = str(obj.get('guid') or '')
            if not guid:
                raw = str(obj.get('id') or '')
                guid = ''.join(ch for ch in raw if ch.isdigit())
            if not guid:
                continue
            name = (obj.get('name')
                    or ' '.join(filter(None, [obj.get('first_name'),
                                              obj.get('last_name')]))
                    or '').strip()
            roster[guid] = name
        total = data.get('total_count')
        LOGGER.info('  curator roster page %d: %d collaborator(s) '
                    '(running total %d%s)', page, len(results), len(roster),
                    f' of {total}' if total else '')
        if total and len(roster) >= int(total):
            break
        if len(results) < 50 and not data.get('next_page'):
            break
        page += 1
        if page > 400:                      # safety valve (~20k curators)
            LOGGER.warning('  curator roster: stopping at page cap')
            break
    return roster


def already_seeded(guid):
    """True if GFDC has EVER counted this profile, at any step -- not
    just whether it's currently in the published Top 50."""
    try:
        MY_DB.connect(reuse_if_open=True)
        cursor = MY_DB.execute_sql(
            'SELECT 1 FROM geni_profiles WHERE profileId = %s LIMIT 1',
            (str(guid),))
        return cursor.fetchone() is not None
    except Exception:
        LOGGER.exception('already_seeded check failed for %s', guid)
        return True   # fail closed: skip rather than risk a duplicate walk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='report the pick, enqueue nothing')
    ap.add_argument('--force', action='store_true',
                    help='enqueue even when the queue is not idle')
    ap.add_argument('--refresh-roster', action='store_true',
                    help='re-fetch the curator roster from Geni instead of '
                         'using the cached copy')
    args = ap.parse_args()

    gfdc_refresh.ensure_schema()

    if not args.force and not args.dry_run and REQUIRE_IDLE and queue_busy():
        LOGGER.info('A calculation is already queued or running -- leaving '
                    'it alone. Nothing to do.')
        return 0

    access_token, refresh_token = gfdc_refresh.load_oauth()
    if not access_token:
        LOGGER.error('No stored Geni tokens. Log in to GFDC once in a '
                     'browser (that stores them), then this will run '
                     'unattended.')
        return 1

    roster = gfdc_refresh.cached_curator_roster()
    if args.refresh_roster or not roster:
        LOGGER.info('Fetching curator roster from Geni (%s)...',
                    'forced refresh' if args.refresh_roster else
                    'cache empty')
        roster = fetch_curator_roster(access_token)
        if roster:
            gfdc_refresh.save_curator_roster(roster)

    pool = dict(load_snapshot_candidates())
    pool.update(roster)   # roster wins on any overlapping guid (has a name)
    LOGGER.info('Candidate pool: %d snapshot + %d curator(s) = %d unique',
                len(load_snapshot_candidates()), len(roster), len(pool))

    todo = [(guid, name) for guid, name in pool.items()
            if not already_seeded(guid)]
    if not todo:
        LOGGER.info('Every candidate has already been run through GFDC at '
                    'least once. Nothing to do.')
        return 0

    LOGGER.info('%d candidate(s) not yet seeded (of %d total)', len(todo),
                len(pool))
    for guid, name in todo[:5]:
        LOGGER.info('  candidate: %-40s %s', (name or '')[:40], guid)

    guid, name = todo[0]
    LOGGER.info('Picked: %s (%s)', name or '?', guid)

    if args.dry_run:
        LOGGER.info('--dry-run: nothing enqueued.')
        return 0

    try:
        profile = json.loads(get_other_profile(access_token, guid))
    except Exception:
        LOGGER.exception('Could not read profile %s from Geni', guid)
        return 1
    profile_id = profile.get('id')
    if not profile_id:
        LOGGER.error('Geni returned no id for guid %s (access denied, or '
                     'the stored token has expired -- log in once to '
                     'refresh it)', guid)
        return 1

    params = {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'email': NOTIFY,
        'other_id': profile_id,
        'includeInTop50': 'on',
        'step_count': SEED_STEPS,
    }
    from rq import Queue, Worker
    queue = Queue(SEED_QUEUE, connection=gfdc_app.CONN)
    try:
        listeners = Worker.count(queue=queue)
    except Exception as exc:
        LOGGER.debug('could not count workers on %r (%s)', SEED_QUEUE, exc)
        listeners = None
    if listeners == 0 and not args.force:
        LOGGER.error("No worker is listening on the %r queue, so this job "
                     "would never run. Nothing enqueued.", SEED_QUEUE)
        return 1

    job = queue.enqueue_call(func='app.create_background_job',
                             args=(params,), timeout=JOB_TIMEOUT)
    LOGGER.info('Enqueued %s steps for %s (%s) on the %r queue as job %s; '
                'results email to %s', SEED_STEPS, name or '?', profile_id,
                SEED_QUEUE, job.id, NOTIFY or '(none)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
