"""Unit tests never load the recipient's live settings, keys or MCP processes."""
import os
import tempfile

_previous=os.environ.get('FINAGENT_DATA_DIR')
_session=tempfile.TemporaryDirectory(prefix='finagent-unit-session-')
os.environ['FINAGENT_DATA_DIR']=_session.name


def pytest_sessionfinish(session,exitstatus):
    if _previous is None:os.environ.pop('FINAGENT_DATA_DIR',None)
    else:os.environ['FINAGENT_DATA_DIR']=_previous
    _session.cleanup()
