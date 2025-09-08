import logging
log = logging.getLogger(__name__)

def emit(event_name: str, **payload):
    log.info("EVENT %s %r", event_name, payload)
