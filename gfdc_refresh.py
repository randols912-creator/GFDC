"""Support for GFDC's unattended Top 50 refresh.

Everything the scheduled refresh needs that the original app has no concept of:

  1. **Persisted Geni credentials.** `app.py` authenticates a queued job with
     the tokens captured from the requesting user's browser session
     (`params['access_token'] = session['access_token']`). A Heroku Scheduler
     dyno has no session, so it has nothing to authenticate with. This module
     keeps the most recent tokens in a `geni_oauth` row, written whenever a
     human logs in, and hands them to the scheduled job. `create_background_job`
     refreshes them as it walks and the job self-heals from there.

  2. **A notion of staleness.** `geni_profiles` records a count but never when
     it was computed, so "recount the oldest" is not expressible. A
     `computedAt` column is added here (idempotently) and stamped every time a
     count is saved.

Kept in its own file so `db.py` and `app.py` each need only a single added
line -- this app is deployed by uploading individual files to GitHub, so small
diffs matter more than tidy layout.
"""
import os
import logging
from datetime import datetime, timezone

from peewee import Model, CharField, IntegerField, DateTimeField, TextField

from db import MY_DB, GeniProfile

LOGGER = logging.getLogger(__name__)

TOP50_STEP = int(os.getenv('GFDC_REFRESH_STEP', '10'))

# How many rows /top50 actually publishes. Imported rather than hardcoded so
# this can never drift from what the site shows.
try:
    from db import STEP_THRESHOLD as PUBLISHED_LIMIT
except ImportError:      # older db.py
    PUBLISHED_LIMIT = 50


class GeniOAuth(Model):
    """Single-row store for the most recently issued Geni tokens.

    Not a per-user table: this app is single-owner in practice, and the
    scheduled refresh only ever needs one working grant. `label` records which
    Geni app the tokens belong to (2134 vs legacy 220) so a token minted under
    one app is never replayed under the other -- Geni rejects that, and it is
    the same trap `GENI_USE_LEGACY_APP` warns about mid-session.
    """
    gid = IntegerField(primary_key=True, default=1)
    label = CharField(null=True)
    access_token = TextField()
    refresh_token = TextField()
    updated_at = DateTimeField(null=True)

    class Meta(object):
        database = MY_DB
        db_table = 'geni_oauth'


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def active_app_label():
    """Which Geni app new logins are using, per GENI_USE_LEGACY_APP."""
    return 'legacy-220' if os.getenv('GENI_USE_LEGACY_APP') == '1' else 'app-2134'


def ensure_schema():
    """Idempotent: safe to call on every boot and every scheduled run."""
    try:
        MY_DB.connect(reuse_if_open=True)
        MY_DB.create_tables([GeniOAuth], safe=True)
    except Exception as exc:
        LOGGER.warning('ensure_schema: could not create geni_oauth (%s)', exc)
    try:
        # peewee does not migrate; MySQL raises 1060 Duplicate column name on
        # the second call, which is the success case from then on.
        MY_DB.execute_sql(
            'ALTER TABLE geni_profiles ADD COLUMN computedAt DATETIME NULL')
        LOGGER.info('ensure_schema: added geni_profiles.computedAt')
    except Exception as exc:
        LOGGER.debug('ensure_schema: computedAt already present (%s)', exc)


def save_oauth(access_token, refresh_token):
    """Record the tokens a human login just produced. Called from app.py's
    /home so simply logging in re-seeds the scheduled refresh -- there is no
    separate seeding ritual to remember."""
    if not (access_token and refresh_token):
        return
    try:
        ensure_schema()
        row = GeniOAuth.get_or_none(GeniOAuth.gid == 1)
        if row is None:
            GeniOAuth.create(gid=1, label=active_app_label(),
                             access_token=access_token,
                             refresh_token=refresh_token, updated_at=_now())
        else:
            GeniOAuth.update(label=active_app_label(),
                             access_token=access_token,
                             refresh_token=refresh_token,
                             updated_at=_now()).where(
                                 GeniOAuth.gid == 1).execute()
        LOGGER.info('save_oauth: stored tokens for %s', active_app_label())
    except Exception:
        LOGGER.exception('save_oauth failed (login itself is unaffected)')


def load_oauth():
    """(access_token, refresh_token) or (None, None).

    Refuses tokens minted under a different Geni app than the one currently
    selected -- replaying those fails in a way that looks like a broken grant
    rather than a mismatched app.
    """
    try:
        ensure_schema()
        row = GeniOAuth.get_or_none(GeniOAuth.gid == 1)
    except Exception:
        LOGGER.exception('load_oauth failed')
        return None, None
    if row is None:
        return None, None
    if row.label and row.label != active_app_label():
        LOGGER.warning('load_oauth: stored tokens are for %s but %s is active '
                       '-- log in once to re-store them',
                       row.label, active_app_label())
        return None, None
    return row.access_token, row.refresh_token


def stamp_computed_at(guid, step):
    """Mark one (profile, step) row as counted now. Called from db.py's
    save_geni_profile, so every count -- interactive or scheduled -- updates
    it and staleness stays honest."""
    try:
        MY_DB.connect(reuse_if_open=True)
        MY_DB.execute_sql(
            'UPDATE geni_profiles SET computedAt = %s '
            'WHERE profileId = %s AND step = %s', (_now(), str(guid), step))
    except Exception as exc:
        LOGGER.debug('stamp_computed_at skipped (%s)', exc)


# runDate is NOT used for staleness. Nothing in the current codebase writes it
# -- /record_count stores counts through save_geni_profile, which never touches
# it -- so it is frozen at whatever the old wnx server last wrote. Confirmed
# 2026-08-17: Brigham Young reads 2015-05-01 even though schoenberg.com's
# crawler recounted the top of this list in August 2026. Ordering by it would
# aim the first runs at the biggest, most expensive trees on the strength of a
# decade-old date that is simply wrong.


def stalest_top50(step=None, limit=None):
    """The PUBLISHED Top 50 at `step`, stalest first.

    Two-stage on purpose. `geni_profiles` holds far more rows than the list
    shows -- 573 at step 10 against a published 50 -- so ranking the whole
    table by staleness would happily pick something at #400 and spend hours
    recounting a number nobody can see. The inner query reproduces exactly what
    /top50 publishes (top N by profile count, same STEP_THRESHOLD the app
    uses); only then does the outer query order that set by staleness.

    Un-stamped rows come first, and among them the CHEAPEST (fewest profiles)
    first. Every row starts un-stamped, so that tiebreak decides the whole
    first pass through the list: smallest-first clears the backlog quickly and
    gets real timestamps onto many rows, where biggest-first would spend the
    opening days on one giant walk. It also avoids redoing the top entries,
    which schoenberg.com's crawler was still recounting in August 2026. Once
    rows carry a computedAt the ordering becomes genuinely oldest-first and the
    tiebreak stops mattering.
    """
    step = TOP50_STEP if step is None else step
    limit = PUBLISHED_LIMIT if limit is None else limit
    rows = []
    try:
        MY_DB.connect(reuse_if_open=True)
        cursor = MY_DB.execute_sql(
            'SELECT profileId, profileName, profiles, computedAt '
            'FROM (SELECT profileId, profileName, profiles, computedAt '
            '      FROM geni_profiles WHERE step = %s '
            '      ORDER BY profiles DESC LIMIT %s) published '
            'ORDER BY (computedAt IS NOT NULL), computedAt ASC, '
            'profiles ASC', (step, limit))
        for guid, name, profiles, computed_at in cursor.fetchall():
            rows.append({'guid': guid, 'name': name,
                         'profiles': profiles, 'computedAt': computed_at})
    except Exception:
        LOGGER.exception('stalest_top50 failed')
    return rows
