#!/usr/bin/env python3
"""Heroku Scheduler entry point: recount the stalest Top 50 profile.

Nothing has been refreshing GFDC's Top 50. The original design had
schoenberg.com's `geni_top10.py --density` do the counting locally and push
results back via `POST /record_count`; that crawler was removed when density
moved to reading GFDC's list instead, so the flow lost its source. Since then
the numbers only move when somebody runs a calculation in the web UI, and
`geni_top10.py` copies whatever is there onto the Geni project page.

This closes the loop inside GFDC, which is the natural home for it: the Redis
queue, the worker dyno and the memory-efficient traversal already exist. One
profile per run, oldest first, so the whole list cycles without ever running
two heavy jobs at once.

    python3 refresh_top50.py            # enqueue one, if the queue is idle
    python3 refresh_top50.py --dry-run  # show what it would pick
    python3 refresh_top50.py --force    # enqueue even if jobs are pending

Suggested schedule: daily. A single Brigham-Young-scale walk can occupy the
worker for many hours (jobs are enqueued with a 7-day timeout), so most runs
will find the queue busy and exit immediately -- that is the intended
behaviour, not a failure.
"""
import os
import sys
import logging
import argparse

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
LOGGER = logging.getLogger('refresh_top50')

# app.py builds the Redis queue and holds the job function; importing it also
# gives us the same rate-limit-aware Geni client the web path uses.
import app as gfdc_app
import gfdc_refresh
from geni_client import get_other_profile

STEP_COUNT = os.getenv('GFDC_REFRESH_STEPS', '10')
NOTIFY = os.getenv('GFDC_REFRESH_EMAIL') or os.getenv('GENI_FROM_ADDR', '')


def queue_busy():
    """True if anything is pending or running.

    Only one worker dyno exists, so a second enqueue cannot start sooner --
    it would only queue behind a job that may run for days, and by the time it
    ran its chosen profile would be a stale choice.
    """
    try:
        pending = gfdc_app.Q.count
    except Exception as exc:
        LOGGER.warning('could not read queue depth (%s); assuming busy', exc)
        return True
    running = 0
    try:
        from rq.registry import StartedJobRegistry
        running = StartedJobRegistry(queue=gfdc_app.Q).count
    except Exception as exc:
        LOGGER.debug('could not read started registry (%s)', exc)
    LOGGER.info('queue: %d pending, %d running', pending, running)
    return bool(pending or running)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='report the pick, enqueue nothing')
    ap.add_argument('--force', action='store_true',
                    help='enqueue even when the queue is not idle')
    args = ap.parse_args()

    gfdc_refresh.ensure_schema()

    if not args.force and not args.dry_run and queue_busy():
        LOGGER.info('A calculation is already queued or running -- leaving it '
                    'alone. Nothing to do.')
        return 0

    rows = gfdc_refresh.stalest_top50()
    if not rows:
        LOGGER.error('No Top 50 rows at step %s -- is geni_profiles populated?',
                     gfdc_refresh.TOP50_STEP)
        return 1

    for r in rows[:5]:
        LOGGER.info('  candidate: %-34s %10s profiles  computedAt=%s',
                    (r['name'] or '')[:34], r['profiles'], r['computedAt'])
    pick = rows[0]
    LOGGER.info('Stalest: %s (guid %s, last counted %s)',
                pick['name'], pick['guid'], pick['computedAt'] or 'never')

    if args.dry_run:
        LOGGER.info('--dry-run: nothing enqueued.')
        return 0

    access_token, refresh_token = gfdc_refresh.load_oauth()
    if not access_token:
        LOGGER.error('No stored Geni tokens. Log in to GFDC once in a browser '
                     '(that stores them), then this will run unattended.')
        return 1

    # Same two-step the web UI performs: the table stores the guid, but
    # create_background_job wants Geni's internal profile id.
    try:
        import json
        profile = json.loads(get_other_profile(access_token, pick['guid']))
    except Exception:
        LOGGER.exception('Could not read profile %s from Geni', pick['guid'])
        return 1
    profile_id = profile.get('id')
    if not profile_id:
        LOGGER.error('Geni returned no id for guid %s (access denied, or the '
                     'stored token has expired -- log in once to refresh it)',
                     pick['guid'])
        return 1

    params = {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'email': NOTIFY,
        'other_id': profile_id,
        'includeInTop50': 'on',      # so each step's count is written back
        'step_count': STEP_COUNT,
    }
    job = gfdc_app.Q.enqueue_call(func='app.create_background_job',
                                  args=(params,), timeout=604800)
    LOGGER.info('Enqueued %s steps for %s (%s) as job %s; results email to %s',
                STEP_COUNT, pick['name'], profile_id, job.id, NOTIFY or '(none)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
