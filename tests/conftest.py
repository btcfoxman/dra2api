import os
from dataclasses import replace

import pytest

os.environ.update(DRA_API_KEY="test-api", DRA_ADMIN_TOKEN="test-admin", DRA_SYNC_TOKEN="test-sync")

from app.config import settings as defaults  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import DRAService  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return replace(defaults, database_path=str(tmp_path / "gateway.db"),
                   chrome_user_data_root=str(tmp_path / "profiles"), poll_interval_seconds=0,
                   request_retries=1, daily_checkin_enabled=False, proxy_pool="",
                   model_map="{}", task_timeout_seconds=5)


@pytest.fixture
def service(settings):
    item = DRAService(Database(settings.database_path), settings)
    yield item
    item.stop()
