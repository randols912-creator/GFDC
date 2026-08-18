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

# GFDC is a public tool -- other people start long calculations through the web
# UI, and there is one worker dyno. This refresh must never make their runs
# wait, so:
#
#   * it enqueues onto the LOW queue. The worker listens on "high, default,
#     low" in that order, and users' jobs go to `default`, so any user job
#     sitting in the queue is dequeued before this one regardless of arrival
#     order.
#   * it refuses to start when anything at all is pending or running, on any
#     queue (see queue_busy).
#   * GFDC_REFRESH_MAX_PROFILES optionally skips profiles above a size, so a
#     scheduled walk can't be one of the multi-day giants.
#
# What none of this can do is preempt: once ANY job is running, later jobs wait
# behind it, because rq priority decides what to dequeue next, not what to
# interrupt. So a scheduled walk that has started will still delay a user who
# arrives mid-walk. The only real fix for that is a second worker dyno; the
# size cap is the cheap approximation, keeping scheduled walks short enough
# that the wait is minutes rather than days.
REFRESH_QUEUE = os.getenv('GFDC_REFRESH_QUEUE', 'low')
# Set GFDC_REFRESH_REQUIRE_IDLE=0 once a dedicated `refresher` dyno serves the
# low queue: with its own dyno the refresh no longer blocks anyone in the
# queue, so waiting for an idle system just wastes days. The one thing still
# shared even then is Geni's per-app rate limit (100 req/10s on app 220), so
# a concurrent guest calculation and refresh will each get roughly half.
REQUIRE_IDLE = os.getenv('GFDC_REFRESH_REQUIRE_IDLE', '1') != '0'
try:
    MAX_PROFILES = int(os.getenv('GFDC_REFRESH_MAX_PROFILES', '0'))
except ValueError:
    MAX_PROFILES = 0


def _queues():
    """Every queue the worker serves, so 'is anything happening?' is honest."""
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
    """True if anything is pending or running on ANY queue.

    Deliberately conservative: a user's calculation may sit on `default` while
    this checks, and starting alongside it would put both through one worker
    and one Geni rate limit. Skipping costs a day; interfering costs someone
    else's run.
    """
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='report the pick, enqueue nothing')
    ap.add_argument('--force', action='store_true',
                    help='enqueue even when the queue is not idle')
    args = ap.parse_args()

    gfdc_refresh.ensure_schema()

    if not args.force and not args.dry_run and REQUIRE_IDLE and queue_busy():
        LOGGER.info('A calculation is already queued or running -- leaving it '
                    'alone. Nothing to do.')
        return 0
    if not REQUIRE_IDLE:
        LOGGER.info('GFDC_REFRESH_REQUIRE_IDLE=0 -- running alongside whatever '
                    'else is queued (dedicated refresher dyno assumed).')

    rows = gfdc_refresh.stalest_top50()
    if not rows:
        LOGGER.error('No Top 50 rows at step %s -- is geni_profiles populated?',
                     gfdc_refresh.TOP50_STEP)
        return 1

    if MAX_PROFILES > 0:
        too_big = [r for r in rows if (r['profiles'] or 0) > MAX_PROFILES]
        rows = [r for r in rows if (r['profiles'] or 0) <= MAX_PROFILES]
        if too_big:
            LOGGER.info('skipping %d profile(s) over GFDC_REFRESH_MAX_PROFILES '
                        '=%d: %s', len(too_big), MAX_PROFILES,
                        ', '.join((t['name'] or '?') for t in too_big[:5]))
        if not rows:
            LOGGER.warning('Every Top 50 profile is above the size cap -- '
                           'nothing to do. Raise GFDC_REFRESH_MAX_PROFILES or '
                           'recount the giants by hand.')
            return 0

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
    from rq import Queue, Worker
    queue = Queue(REFRESH_QUEUE, connection=gfdc_app.CONN)

    # Refuse to enqueue into a queue nothing serves. With a dedicated
    # `refresher` dyno the refresh lives on its own queue -- which means
    # scaling that dyno to 0 (or never scaling it up) would leave jobs sitting
    # in Redis forever with no error anywhere. Better to fail loudly here.
    try:
        listeners = Worker.count(queue=queue)
    except Exception as exc:
        LOGGER.debug('could not count workers on %r (%s)', REFRESH_QUEUE, exc)
        listeners = None
    if listeners == 0 and not args.force:
        LOGGER.error("No worker is listening on the %r queue, so this job "
                     "would never run. Either scale the refresher dyno "
                     "(heroku ps:scale refresher=1) or point this at a served "
                     "queue (GFDC_REFRESH_QUEUE=default). Nothing enqueued.",
                     REFRESH_QUEUE)
        return 1

    job = queue.enqueue_call(func='app.create_background_job',
                             args=(params,), timeout=604800)
    LOGGER.info('Enqueued %s steps for %s (%s) on the %r queue as job %s; '
                'results email to %s', STEP_COUNT, pick['name'], profile_id,
                REFRESH_QUEUE, job.id, NOTIFY or '(none)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
