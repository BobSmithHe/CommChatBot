from __future__ import annotations

from pathlib import Path
import sys
from urllib.parse import quote_plus

from sqlalchemy import MetaData, create_engine, insert, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infra.config import get_settings
from app.infra.database import Base


def main() -> None:
    settings = get_settings()
    source_path = Path(settings.sqlite_path)
    if not source_path.is_file():
        print("SQLite source not found; MySQL schema only")
    source = create_engine(f"sqlite:///{source_path}")
    target_url = (
        f"mysql+pymysql://{quote_plus(settings.db_user)}:{quote_plus(settings.db_password)}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
    )
    target = create_engine(target_url, pool_pre_ping=True)
    Base.metadata.create_all(target)
    if not source_path.is_file():
        return

    source_meta, target_meta = MetaData(), MetaData()
    source_meta.reflect(bind=source)
    target_meta.reflect(bind=target)
    copied = 0
    with source.connect() as source_connection, target.begin() as target_connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in source_meta.tables or table.name not in target_meta.tables:
                continue
            source_table, target_table = source_meta.tables[table.name], target_meta.tables[table.name]
            primary_keys = [column.name for column in target_table.primary_key.columns]
            existing = {
                tuple(row)
                for row in target_connection.execute(select(*(target_table.c[name] for name in primary_keys)))
            } if primary_keys else set()
            rows = source_connection.execute(select(source_table)).mappings()
            for row in rows:
                values = {key: value for key, value in dict(row).items() if key in target_table.c}
                key = tuple(values.get(name) for name in primary_keys)
                if primary_keys and key in existing:
                    continue
                target_connection.execute(insert(target_table).values(**values))
                existing.add(key)
                copied += 1
    print(f"Migrated {copied} rows to MySQL")


if __name__ == "__main__":
    main()
