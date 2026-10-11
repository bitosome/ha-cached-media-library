"""Bounded query discovery; availability always remains the Store's decision."""
import queue
import threading
import time


class SearchJobs:
    def __init__(self, discover, stop, max_pending=16, max_recent=128):
        self.discover = discover
        self.stop = stop
        self.queue = queue.Queue(maxsize=max_pending)
        self.max_recent = max_recent
        self.lock = threading.Lock()
        self.jobs = {}

    def submit(self, kind, query):
        if (kind not in ('movie', 'series') or not isinstance(query, str)
                or not 2 <= len(query.strip()) <= 200 or not any(c.isalnum() for c in query)):
            return None
        query = ' '.join(query.split())
        key = kind, query.casefold()
        now = time.monotonic()
        with self.lock:
            if self.stop.is_set():
                return None
            # Pending work is never evicted, so repeated queries cannot duplicate it.
            self.jobs = {k: j for k, j in self.jobs.items()
                         if not j['done'].is_set() or j['expires'] > now}
            if key in self.jobs:
                return self.jobs[key]
            if len(self.jobs) >= self.max_recent:
                finished = [k for k, j in self.jobs.items() if j['done'].is_set()]
                if not finished:
                    return None
                del self.jobs[finished[0]]
            job = {'kind': kind, 'query': query, 'done': threading.Event(),
                   'expires': 0, 'ids': [], 'error': False}
            try:
                self.queue.put_nowait(job)
            except queue.Full:
                return None
            self.jobs[key] = job
            return job

    def run_one(self, timeout=1):
        try:
            job = self.queue.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            if self.stop.is_set():
                job['error'] = True
            else:
                job['ids'] = self.discover(job['kind'], job['query'])
        except Exception:
            # Neither credential-bearing request URLs nor query text go to logs.
            job['error'] = True
        finally:
            # Failures must retry soon, not become a cached "no results" verdict.
            job['expires'] = time.monotonic() + (30 if job['error'] else 600)
            job['done'].set()
            self.queue.task_done()
        return True
