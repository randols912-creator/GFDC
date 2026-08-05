# GFDC - Python 3 port (for Heroku)

This is the Geni Forest Density Calculator ported from Python 2.7 to Python 3,
with a modernized dependency set so it builds on current Heroku stacks.
Original outage cause: Geni's firewall (Imperva) blocks the old wnx server's IP;
running from a new host (Heroku dyno) with a fresh IP is the fix.

## Code changes made
- app.py: removed `from sets import Set`; `Set()` -> `set()`; fixed `print error`
  -> `print(error)`; normalized 5 tab-indented lines to spaces (py3 forbids mixing).
- app.py: replaced unmaintained Flask-KVSession/simplekv session store with
  Flask-Session (SESSION_TYPE='filesystem'), which reproduces the old in-memory,
  per-process session behavior on a modern Flask.
- app.py: made the rq_dashboard admin panel optional and updated it to the modern
  blueprint API (guarded by try/except so a version mismatch can't crash the app;
  mounted at /rq).
- geni_client.py: `.iteritems()` -> `.items()`.
- db.py: added `pymysql.install_as_MySQLdb()` shim so peewee uses PyMySQL;
  `MY_DB.connect()` -> `MY_DB.connect(reuse_if_open=True)` (peewee 3 semantics);
  `PrimaryKeyField` -> `AutoField` (peewee 3 rename).
- worker.py: reads REDIS_URL first, then REDISTOGO_URL (Heroku Redis compatibility).

## Validation done (in AWS CloudShell, Python 3.13)
- All files byte-compile under Python 3 (py_compile).
- `pip install -r requirements.txt` resolves with no conflicts.
- Every module imports: flask, flask_session, peewee, rq, rq_dashboard, redis,
  pymysql, requests, and the app's setenvs/geni_client/mail/worker/db.
- `import app` runs the whole app and stops only at the live MySQL connection
  (expected - no database in the sandbox). No code/syntax/import errors remain.
- NOT yet tested against a live MySQL, Redis, or the Geni API - do a smoke test
  after first deploy.

## Deploy on Heroku
1. Add-ons: a MySQL provider (JawsDB or ClearDB) + Heroku Data for Redis.
2. Migrate the MySQL data from the old server (schema: TopProfiles, GeniProfile, GeniJob).
3. Set config vars (values are in the old server's setenv.sh):
   GENI_CLIENT_ID, GENI_CLIENT_SECRET, GENI_DB_HOST, GENI_DB_NAME, GENI_DB_USER,
   GENI_DB_PASSWD, GENI_FROM_ADDR, GENI_MAILGUN_API_KEY, GENI_MAILGUN_URL,
   GENI_REDIRECT_URL (point at the new domain, e.g. https://<app>.herokuapp.com/home),
   and REDIS_URL (usually set automatically by the Redis add-on).
4. In your Geni developer app, add the new redirect URI/domain to the allow-list.
   (Public client id: 5VqX0578AOq8tvX4LG6C9P7v7A8lWHMSaEv3uKLT — Geni App 2134)
5. Deploy, then: heroku ps:scale web=1 worker=1
6. Smoke test: log in via Geni, run a density calculation.

Procfile, requirements.txt, runtime.txt and .python-version (3.12) are included.
The original Python 2 requirements are kept as requirements.legacy.txt for reference.
