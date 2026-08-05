"""Настройка Alembic под ФинИгроСкоп.

Отличий от шаблона два, и оба принципиальные:

1. **Адрес базы берётся из config.py, а не из alembic.ini.** Путь зависит от
   режима запуска (у тестового прогона своя база), и дублировать эту логику в
   ini-файле значило бы завести второй источник правды, который рано или поздно
   разойдётся с первым.

2. **`render_as_batch=True`.** SQLite не умеет ALTER TABLE для смены типа,
   ограничений и удаления колонок. Batch-режим Alembic обходит это, пересоздавая
   таблицу и перенося данные, — без него почти любая правка схемы на SQLite
   просто не применится.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Каталог migrations/ лежит внутри проекта, но запускается Alembic'ом из своего
# окружения — корень проекта в sys.path не попадает сам.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as app_config  # noqa: E402
import models  # noqa: E402,F401 — импорт регистрирует таблицы в metadata
from models import db  # noqa: E402

alembic_config = context.config
alembic_config.set_main_option("sqlalchemy.url", app_config.database_uri())

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = db.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=alembic_config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
