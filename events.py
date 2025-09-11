import logging
from collections import defaultdict
log = logging.getLogger(__name__)

_subs = defaultdict(list)

def on(name):
    def deco(fn):
        _subs[name].append(fn)
        return fn
    return deco

def emit(name, **kwargs):
    # Log the event (old behavior)
    log.info("EVENT %s %r", name, kwargs)
    # Call subscribers (new behavior)
    for fn in _subs.get(name, []):
        try:
            fn(name, **kwargs)
        except Exception:
            import traceback; traceback.print_exc()
