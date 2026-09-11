#!/usr/bin/env python3
"""One-off (or occasional) backfill: fill in geni_profiles.claimed for rows
that predate the claimed/unclaimed feature (2026-09-11), or that otherwise
never got checked (e.g. a job's claimed check failed and was never retried).

Cheap: one API call per profile (geni_client.fetch_claimed), no walking, no
rate-limit-heavy BFS. Only touches the PUBLISHED Top 50 by default (the same
set /top50 actually shows), not the long tail of rows geni_profiles
accumulates below it.

    python3 backfill_claimed.py            # published Top 50 only
    python3 backfill_claimed.py --all      # every distinct profileId with a
                                            # NULL claimed row, at any step
    python3 backfill_claimed.py --dry-run  # show who's missing, do nothing

Run once by hand after deploying this feature:
    heroku run python3 backfill_claimed.py -a gfdc
Safe to re-run any time -- it only ever touches NULL rows, and stamp_claimed
overwrites are idempotent.
"""
import sys
import logging
import argparse

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
LOGGER = logging.getLogger('backfill_claimed')

import gfdc_refresh
from db import MY_DB
from geni_client import fetch_claimed

TOP50_STEP = gfdc_refresh.TOP50_STEP
PUBLISHED_LIMIT = gfdc_refresh.PUBLISHED_LIMIT


def _missing_published():
    """(profileId, name) for the published Top 50 that has never been
    claimed-checked. Two-stage, same idea as gfdc_refresh.stalest_top50:
    reproduce exactly what /top50 publishes, then filter to NULL."""
    rows = []
    MY_DB.connect(reuse_if_open=True)
    cursor = MY_DB.execute_sql(
        'SELECT profileId, profileName FROM '
        '(SELECT profileId, profileName, profiles, claimed FROM geni_profiles '
        ' WHERE step = %s ORDER BY profiles DESC LIMIT %s) published '
        'WHERE claimed IS NULL', (TOP50_STEP, PUBLISHED_LIMIT))
    for guid, name in cursor.fetchall():
        rows.append((guid, name))
    return rows


def _missing_all():
    rows = []
    MY_DB.connect(reuse_if_open=True)
    cursor = MY_DB.execute_sql(
        'SELECT DISTINCT profileId, profileName FROM geni_profiles '
        'WHERE claimed IS NULL')
    for guid, name in cursor.fetchall():
        rows.append((guid, name))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true',
                    help='every NULL-claimed profileId, not just the published Top 50')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    gfdc_refresh.ensure_schema()

    access_token, refresh_token = gfdc_refresh.load_oauth()
    if not access_token:
        LOGGER.error('No stored Geni tokens. Log in to GFDC once in a browser '
                     '(that stores them), then re-run this.')
        return 1

    todo = _missing_all() if args.all else _missing_published()
    if not todo:
        LOGGER.info('Nothing to backfill -- every %s profile already has a '
                    'claimed status.', 'tracked' if args.all else 'published Top 50')
        return 0

    LOGGER.info('%d profile(s) need a claimed-status check%s',
                len(todo), ' (dry run)' if args.dry_run else '')
    if args.dry_run:
        for guid, name in todo:
            LOGGER.info('  would check: %-40s %s', (name or '')[:40], guid)
        return 0

    checked = failed = 0
    for i, (guid, name) in enumerate(todo, 1):
        LOGGER.info('[%d/%d] %s (%s)', i, len(todo), name or '?', guid)
        claim = fetch_claimed(access_token, guid)
        if claim is None:
            failed += 1
            continue
        gfdc_refresh.stamp_claimed(guid, claim)
        checked += 1
        LOGGER.info('  -> %s', 'claimed' if claim else 'unclaimed')

    LOGGER.info('Done: %d checked, %d failed (left NULL, will retry next run)',
                checked, failed)
    return 0


if __name__ == '__main__':
    sys.exit(main())
