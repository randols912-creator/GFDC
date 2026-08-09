#!/usr/bin/env python3
"""Unattended rescan of GFDC's Top 20 (of the Top 50) at 10 steps, roughly
every ~60 days -- trees grow over time, so the giant-tree candidates we
already know about are worth refreshing periodically, without a human
sitting at the Calculator page.

Two entry points in this one file, same pattern as worker.py:
  - run as a script (`python rescan_top20.py`, via Heroku Scheduler, daily):
    self-gates on RESCAN_INTERVAL_DAYS, then enqueues ONE background job
    covering all 20 profiles.
  - imported by the RQ worker to resolve `rescan_top20.run_rescan_job`,
    the actual job body.

Why one job for all 20 profiles instead of 20 separate jobs: Geni's OAuth
issues a new access_token *and* a new refresh_token on every refresh
(get_profile_details reassigns both from the refresh response). With a
single worker dyno, jobs run strictly one at a time anyway, but 20
separately-enqueued jobs would each start from the *same* token snapshot
taken once up front -- by the time job #5 actually starts, jobs #1-4 may
already have rotated that refresh_token out from under it. Looping over
all 20 profiles inside one job carries the access/refresh token forward in
local variables between profiles, exactly like create_background_job
already does between steps of one profile -- no cross-job handoff, no
race.
"""
import os, sys, json, logging

from setenvs import set_configs
set_configs()

from datetime import datetime, timezone

from geni_client import get_refreshed_token
from db import get_service_token, save_service_token, set_last_rescan, \
    get_top50_profiles, save_geni_profile, setup_db
from rq import Queue
from worker import CONN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
LOGGER = logging.getLogger("rescan_top20")

SERVICE_LABEL = 'rschoenberg@jewishgen.org'   # keep in sync with app.py's SERVICE_AUTH_LABEL
RESCAN_INTERVAL_DAYS = 60
STEP_COUNT = 10
TOP_N = 20

Q = Queue(connection=CONN)


def _days_since(iso_ts):
    if not iso_ts:
        return None
    then = datetime.fromisoformat(iso_ts)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def run_rescan_job(profile_ids, access_token, refresh_token):
    """The actual RQ job body -- resolved as 'rescan_top20.run_rescan_job'
    by the worker. Crawls each profile's full STEP_COUNT-step neighborhood
    in turn, saving into geni_profiles same as a manual Calculator
    submission, and persists whatever the token pair ends up as when
    everything finishes (it may have rotated several times along the way)
    so the *next* scheduled run starts from a still-valid pair."""
    # Imported here, not at module load: app.py builds a Flask app and a
    # second Queue() at import time, which is unnecessary (and a little
    # wasteful) for the Scheduler-invoked script path above, which never
    # calls this function directly -- only the worker process, resolving
    # this job, needs it.
    from app import get_step_profiles_thread

    LOGGER.info("Rescan job starting: %d profile(s) at %d steps", len(profile_ids), STEP_COUNT)
    for i, profile_id in enumerate(profile_ids, 1):
        visited_set = set()
        local_session = {}
        LOGGER.info("[%d/%d] %s", i, len(profile_ids), profile_id)
        for step in range(0, STEP_COUNT):
            step_data = get_step_profiles_thread(
                access_token, refresh_token, step, visited_set, profile_id, local_session)
            access_token = step_data['access_token']
            refresh_token = step_data['refresh_token']
            save_geni_profile(
                step_data, local_session['stepProfileName'],
                local_session['guid'], local_session['stepUserLink'])
        LOGGER.info("  -> %s: %s profiles at step %d",
                    local_session.get('stepProfileName'), step_data['total'], STEP_COUNT)
        # Save progress after every profile, not just at the end, so a
        # mid-run crash doesn't lose an already-rotated refresh_token.
        save_service_token(SERVICE_LABEL, access_token, refresh_token,
                           datetime.now(timezone.utc).isoformat())

    LOGGER.info("Rescan job done.")


def main():
    setup_db()
    tok = get_service_token(SERVICE_LABEL)
    if not tok or not tok.get('refreshToken'):
        LOGGER.error("No service token saved yet for %s -- visit /service-login "
                    "once first (an email with that link should already be on "
                    "its way, or trigger request_service_login_email()).", SERVICE_LABEL)
        return 1

    since = _days_since(tok.get('lastRescanAt'))
    if since is not None and since < RESCAN_INTERVAL_DAYS:
        LOGGER.info("Last rescan was %d day(s) ago (< %d); nothing to do.",
                    since, RESCAN_INTERVAL_DAYS)
        return 0

    LOGGER.info("Refreshing service token before enqueuing...")
    refreshed = json.loads(get_refreshed_token(tok['refreshToken']))
    access_token = refreshed['access_token']
    refresh_token = refreshed.get('refresh_token', tok['refreshToken'])
    now = datetime.now(timezone.utc).isoformat()
    save_service_token(SERVICE_LABEL, access_token, refresh_token, now)

    profiles = get_top50_profiles(STEP_COUNT)[:TOP_N]
    if not profiles:
        LOGGER.warning("No %d-step profiles found in the DB; nothing to rescan.", STEP_COUNT)
        return 0
    profile_ids = [p['profileId'] for p in profiles]

    LOGGER.info("Enqueuing a rescan of %d profile(s): %s", len(profile_ids),
                ", ".join(p['profileName'] for p in profiles))
    Q.enqueue_call(func='rescan_top20.run_rescan_job',
                   args=(profile_ids, access_token, refresh_token),
                   timeout=604800)

    # Set now, not after the job finishes -- a 20-profile crawl can run for
    # days, and we don't want a slow Scheduler tick in between to look like
    # "nothing happened yet" and enqueue a second overlapping rescan.
    set_last_rescan(SERVICE_LABEL, now)
    return 0


if __name__ == '__main__':
    sys.exit(main())
