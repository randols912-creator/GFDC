"""Resumable walks for GFDC's long background jobs (October 2026).

Why this exists
---------------
A Top 50 walk holds its whole state -- the last two BFS layers and the one
being built -- in the worker's memory. Anything that ends the process throws
all of it away: Heroku restarts every dyno at least once a day, every deploy
and every config-var change restarts them too, and a single failed Geni call
used to end the job outright. Because the refresher always picks the stalest
profile, and an unfinished walk never stamps step 10, the same profile was
picked again and again from step 1 -- the "Strickler keeps starting over"
symptom -- and nothing else on the list was ever recounted.

This module saves the walk to Redis as it goes (after every step, and every
few minutes inside a step), so a killed job picks up where it stopped the
next time the scheduler enqueues it.

Size
----
Layers can hold millions of profile ids. Geni ids look like
"profile-6000000012345678901"; we split off the text prefix, sort the
numbers, and store the gaps between them as varints, then zlib the lot --
roughly 2-4 bytes an id instead of ~28. A checkpoint larger than
GFDC_CKPT_MAX_BYTES (default 20 MB) is skipped with a warning rather than
risk filling Redis; the walk itself carries on regardless.

Nothing here is allowed to break a walk: every Redis failure is logged and
swallowed, and a checkpoint that will not decode is discarded.
"""
import os
import re
import time
import zlib
import json
import logging

LOGGER = logging.getLogger(__name__)

VERSION = 1
TTL_SECONDS = int(os.getenv('GFDC_CKPT_TTL_DAYS', '30')) * 86400
MAX_BYTES = int(os.getenv('GFDC_CKPT_MAX_BYTES', str(20 * 1024 * 1024)))
SAVE_EVERY_SECONDS = int(os.getenv('GFDC_CKPT_EVERY_SECONDS', '600'))

_ID_RE = re.compile(r'^(\D*)(\d+)$')


def _conn():
    from worker import CONN      # late import: worker builds the connection
    return CONN


def key_for(other_id, step_count):
    return 'gfdc:walk:%s:%s' % (other_id, step_count)


def meta_key_for(other_id, step_count):
    return 'gfdc:walkmeta:%s:%s' % (other_id, step_count)


# ---------------------------------------------------------------- encoding

def _varint(n, out):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _unvarints(buf):
    vals, n, shift = [], 0, 0
    for b in buf:
        n |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
        else:
            vals.append(n)
            n, shift = 0, 0
    return vals


def encode_ids(ids):
    """Set/list of ids (str or int) -> bytes. Exact round trip, including type."""
    groups, other = {}, []
    for i in ids:
        if isinstance(i, int):
            groups.setdefault(('i', ''), []).append(i)
            continue
        m = _ID_RE.match(i) if isinstance(i, str) else None
        # Leading zeros would not survive int(); keep those verbatim.
        if m and not (len(m.group(2)) > 1 and m.group(2)[0] == '0'):
            groups.setdefault(('s', m.group(1)), []).append(int(m.group(2)))
        else:
            other.append(i)
    header, blobs = [], []
    for (kind, prefix), nums in groups.items():
        nums.sort()
        out, prev = bytearray(), 0
        for n in nums:
            _varint(n - prev, out)
            prev = n
        header.append([kind, prefix, len(nums), len(out)])
        blobs.append(bytes(out))
    head = json.dumps({'g': header, 'o': other}).encode()
    return zlib.compress(len(head).to_bytes(4, 'big') + head + b''.join(blobs), 6)


def decode_ids(data):
    raw = zlib.decompress(data)
    hlen = int.from_bytes(raw[:4], 'big')
    head = json.loads(raw[4:4 + hlen])
    pos, out = 4 + hlen, set(head['o'])
    for kind, prefix, count, blen in head['g']:
        gaps = _unvarints(raw[pos:pos + blen])
        pos += blen
        if len(gaps) != count:
            raise ValueError('checkpoint id block is corrupt')
        n = 0
        for g in gaps:
            n += g
            out.add(n if kind == 'i' else prefix + str(n))
    return out


# ------------------------------------------------------------- save / load

def save(other_id, step_count, state):
    """state: dict with 'meta' (JSON-able) and id collections under
    'layer_prev', 'layer_frontier', 'new_layer' (any may be None)."""
    try:
        parts = {}
        for name in ('layer_prev', 'layer_frontier', 'new_layer'):
            val = state.get(name)
            parts[name] = encode_ids(val) if val is not None else None
        meta = dict(state['meta'])
        meta['v'] = VERSION
        meta['saved_at'] = time.time()
        meta_bytes = json.dumps(meta).encode()
        size = len(meta_bytes) + sum(len(p) for p in parts.values() if p)
        if size > MAX_BYTES:
            LOGGER.warning('checkpoint for %s is %.1f MB, over the %.1f MB cap '
                           '-- not saved (walk continues)', other_id,
                           size / 1e6, MAX_BYTES / 1e6)
            return False
        conn = _conn()
        k = key_for(other_id, step_count)
        mapping = {'meta': meta_bytes}
        for name, blob in parts.items():
            mapping[name] = blob if blob is not None else b''
        pipe = conn.pipeline()
        pipe.delete(k)
        pipe.hset(k, mapping=mapping)
        pipe.expire(k, TTL_SECONDS)
        # Small side key so refresh_top50 can find in-progress walks
        # without downloading megabytes of layers.
        pipe.set(meta_key_for(other_id, step_count), json.dumps({
            'other_id': other_id, 'guid': meta.get('guid'),
            'name': meta.get('stepProfileName'),
            'completed_steps': meta.get('completed_steps'),
            'pos': meta.get('pos'), 'saved_at': meta['saved_at'],
        }), ex=TTL_SECONDS)
        pipe.execute()
        LOGGER.info('checkpoint saved for %s: %s step(s) done, position %s, '
                    '%.1f MB', meta.get('stepProfileName') or other_id,
                    meta.get('completed_steps'), meta.get('pos'), size / 1e6)
        return True
    except Exception:
        LOGGER.exception('checkpoint save failed for %s (walk continues)', other_id)
        return False


def load(other_id, step_count):
    """The saved state (same shape as save()'s), or None."""
    try:
        raw = _conn().hgetall(key_for(other_id, step_count))
    except Exception:
        LOGGER.exception('checkpoint load failed for %s; starting fresh', other_id)
        return None
    if not raw:
        return None
    try:
        raw = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
        meta = json.loads(raw['meta'])
        if meta.get('v') != VERSION:
            raise ValueError('checkpoint version %r' % meta.get('v'))
        state = {'meta': meta}
        for name in ('layer_prev', 'layer_frontier', 'new_layer'):
            blob = raw.get(name)
            state[name] = decode_ids(blob) if blob else None
        return state
    except Exception:
        LOGGER.exception('checkpoint for %s is unreadable; discarding it', other_id)
        clear(other_id, step_count)
        return None


def clear(other_id, step_count):
    try:
        _conn().delete(key_for(other_id, step_count),
                       meta_key_for(other_id, step_count))
    except Exception:
        LOGGER.exception('checkpoint clear failed for %s', other_id)


def in_progress(step_count):
    """Metadata of every saved walk at this step count, newest first."""
    out = []
    try:
        conn = _conn()
        for k in conn.scan_iter(match='gfdc:walkmeta:*:%s' % step_count, count=200):
            v = conn.get(k)
            if v:
                try:
                    out.append(json.loads(v))
                except ValueError:
                    pass
    except Exception:
        LOGGER.exception('could not list in-progress walks')
    out.sort(key=lambda m: m.get('saved_at') or 0, reverse=True)
    return out
