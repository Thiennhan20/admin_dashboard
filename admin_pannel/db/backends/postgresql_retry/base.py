import logging
import os
import time

from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper


logger = logging.getLogger(__name__)


class DatabaseWrapper(PostgresDatabaseWrapper):
    def get_new_connection(self, conn_params):
        timeout = int(os.environ.get('DB_WAIT_TIMEOUT', '120'))
        interval = float(os.environ.get('DB_WAIT_INTERVAL', '3'))
        deadline = time.monotonic() + timeout
        attempt = 1
        last_error = None

        while time.monotonic() < deadline:
            try:
                return super().get_new_connection(conn_params)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    'Database connection failed. Retry %s in %ss. Error: %s',
                    attempt,
                    interval,
                    exc,
                )
                time.sleep(interval)
                attempt += 1

        if last_error:
            raise last_error
        return super().get_new_connection(conn_params)
