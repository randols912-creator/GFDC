# pylint: disable=line-too-long
"""Worker creates worker threads that listen to redis queue for jobs to process"""

import os, sys, logging, logging.config
import redis
from rq import Worker, Queue, Connection

LOGGER = logging.getLogger(__name__)

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


REDIS_URL = os.getenv('REDIS_URL') or os.getenv('REDISTOGO_URL', 'redis://localhost:6379')

def get_redis_url():
    return REDIS_URL

try:
    _url = get_redis_url()
    CONN = redis.from_url(_url, ssl_cert_reqs=None) if _url.startswith("rediss://") else redis.from_url(_url)
except:
    LOGGER.error("redis connection error: %s", sys.exc_info()[0])

if __name__ == '__main__':
    # Which queues this process serves. Was hardcoded to all three, which meant
    # every dyno competed for every job. Now it can be split so guest
    # calculations get a worker of their own:
    #
    #   worker:    python worker.py high default   <- guest jobs from the web UI
    #   refresher: python worker.py low            <- the scheduled Top 50 refresh
    #
    # With no arguments it still listens on all three, so a single-dyno setup
    # (and anyone running this locally) behaves exactly as before.
    LISTEN = sys.argv[1:] or os.getenv('RQ_QUEUES', 'high default low').split()
    LOGGER.info("Starting workers on: %s", " ".join(LISTEN))

    with Connection(CONN):
        # qs = map(Queue, LISTEN) or [Queue()]
        WORKER = Worker([Queue(queue_name) for queue_name in LISTEN])
        WORKER.work()
