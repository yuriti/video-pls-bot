"""Short-lived, single-use extraction handoff inside one private worker."""

import copy
import json
import secrets
import time
from collections import OrderedDict


class MetadataHandoff:
    def __init__(self, *, ttl=120, limit=8, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.entries = OrderedDict()

    def expire(self):
        now = self.clock()
        for key, (expires, _, _) in list(self.entries.items()):
            if expires <= now:
                del self.entries[key]

    def put(self, url, info):
        self.expire()
        # Bound memory as well as count. Never persist or log signed URLs.
        if len(json.dumps(info, default=str).encode()) > 2_000_000:
            return None
        key = secrets.token_hex(16)
        self.entries[key] = (self.clock() + self.ttl, url, copy.deepcopy(info))
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return key

    def take(self, key, url):
        self.expire()
        entry = self.entries.pop(key, None)
        return entry[2] if entry and entry[1] == url else None


handoff = MetadataHandoff()
