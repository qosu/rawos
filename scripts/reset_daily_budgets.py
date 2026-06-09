#!/usr/bin/env python3
"""Reset all users' daily token budgets. Run via systemd timer at midnight UTC."""
import sys
sys.path.insert(0, '/root/rawos')

import datetime
import logging
import rawos.db as db
from rawos.config import settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('rawos.reset_daily_budgets')

db.init(settings.db_path)

users = db.get_all_users(limit=10000)
today = datetime.date.today().isoformat()

reset_count = 0
for user in users:
    try:
        db.reset_daily_budget(user.id)
        reset_count += 1
    except Exception as e:
        log.error('failed to reset budget for user %s: %s', user.id, e)

log.info('reset daily budgets for %d users on %s', reset_count, today)
