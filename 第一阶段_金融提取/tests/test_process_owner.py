import os
from finance_extract.app import owner_alive
def test_live_owner_is_preserved_without_signal():
    assert owner_alive(os.getpid()) is True
def test_invalid_owner_is_not_live():
    assert owner_alive(None) is False
    assert owner_alive(0) is False
