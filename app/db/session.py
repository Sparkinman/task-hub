"""Database engine, session management and schema creation."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DB_PATH, ensure_directories
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection, _record) -> None:
    """Apply SQLite pragmas on every new connection.

    Write-ahead logging is the important one. Task Hub reads the database from
    web requests while the sync engine writes to it from a background thread,
    and under the default rollback journal those readers and that writer block
    each other -- the page would stall for the duration of a sync.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    # Wait rather than immediately raising "database is locked" when the sync
    # engine holds the write lock.
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        ensure_directories()
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            # Sessions are handed to threadpool workers, which are not the
            # thread that created the connection.
            connect_args={"check_same_thread": False},
            future=True,
        )
        event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


#: Columns added after the first release. SQLite can add a column to an
#: existing table but cannot alter or drop one, so every entry here must be
#: nullable or carry a default. Applied in order on every startup.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("item_links", "last_pushed_fields", "JSON"),
    ("item_links", "sync_group_id", "INTEGER"),
    # DEFAULT 1 so every mapping that existed before this column keeps behaving
    # exactly as it did: reading a list could always introduce new tasks.
    ("list_mappings", "create_from_remote", "BOOLEAN DEFAULT 1"),
    # Null means "no restriction", which is what every existing row wants: an
    # upgrade must not stop a destination that was working from being written to.
    ("list_mappings", "write_from_list_ids", "JSON"),
    ("items", "origin_remote_list_id", "INTEGER"),
    # Null on existing rows, which is correct: a tombstone written before this
    # column existed has no record of the remote ids, and falls back to the
    # UID match it has always used.
    ("tombstones", "remote_ids", "JSON"),
    # Null on existing rows, which is correct: an account connected before this
    # column existed has no record of the address it was connected at, and a
    # guess would produce a warning about a move that may never have happened.
    ("accounts", "connected_redirect_uri", "VARCHAR(500)"),
    # Defaults to 0, which is right for every list discovered before this
    # existed: no service had declared one read-only, so none was.
    ("remote_lists", "read_only", "BOOLEAN DEFAULT 0"),
    # Null on rows backed up before previews existed. They are filled in on the
    # next pass from the PDFs already on disk, with no network involved.
    ("supernote_notes", "thumb_name", "VARCHAR(128)"),
    # Nothing was excluded before this existed, so 0 is right for every row.
    ("supernote_notes", "excluded", "BOOLEAN DEFAULT 0"),
]


def _apply_column_migrations(engine: Engine) -> None:
    """Add columns that newer code expects but an older database lacks.

    Task Hub has to upgrade itself. Its user does not run migration commands,
    so an upgrade that needed one would simply break the installation. Keeping
    schema changes to additive columns means an upgrade is always safe and
    always automatic.
    """
    with engine.begin() as connection:
        for table, column, coltype in _COLUMN_MIGRATIONS:
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # Table does not exist yet; create_all will make it.
            if column in existing:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
            )
            logger.info("Added column %s.%s", table, column)


def _close_interrupted_runs() -> None:
    """Mark syncs left mid-flight by a restart as failed.

    A run only leaves "running" when its own code finishes, so a container
    stopped mid-sync leaves a row that claims to still be in progress. The
    interface would then show a sync that never ends.
    """
    from sqlalchemy import select as _select

    from app.db.models import SyncOutcome, SyncRun, utcnow

    session_factory = get_session_factory()
    session = session_factory()
    try:
        stale = session.execute(
            _select(SyncRun).where(SyncRun.outcome == SyncOutcome.RUNNING)
        ).scalars().all()
        for run in stale:
            run.outcome = SyncOutcome.FAILED
            run.finished_at = run.finished_at or utcnow()
        if stale:
            session.commit()
            logger.info("Closed %d sync run(s) interrupted by a restart", len(stale))
    finally:
        session.close()


#: Tables whose UNIQUE constraint changed after release, with the columns the
#: constraint should now cover. SQLite cannot alter a constraint in place, so
#: the table is rebuilt: create the new shape, copy every row across, swap.
#:
#: Detection is by columns rather than by index name, because SQLite does not
#: keep the name of a table-level UNIQUE constraint -- it calls the index
#: sqlite_autoindex_<table>_<n> regardless of what the constraint was called.
_CONSTRAINT_REBUILDS: list[tuple[str, tuple[str, ...]]] = [
    ("items", ("uid", "sync_group_id")),
    ("item_links", ("account_id", "remote_id", "sync_group_id")),
]


def _unique_index_columns(conn, table_name: str) -> list[set[str]]:
    """Column sets covered by each UNIQUE index on a table."""
    found: list[set[str]] = []
    for row in conn.exec_driver_sql(f"PRAGMA index_list({table_name})"):
        name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        columns = {
            info[2] for info in conn.exec_driver_sql(f'PRAGMA index_info("{name}")')
        }
        found.append(columns)
    return found


def _backup_database(reason: str) -> None:
    """Copy the database aside before a structural change.

    Rebuilding a table is the only operation here that could lose data if it
    were interrupted. A timestamped copy costs a few megabytes and makes the
    whole thing recoverable, which matters because the person running this
    cannot be asked to restore from a shell.
    """
    import shutil

    if not DB_PATH.exists():
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = DB_PATH.with_name(f"{DB_PATH.name}.backup-{stamp}-{reason}")
    if target.exists():
        return
    shutil.copy2(DB_PATH, target)
    logger.info("Database backed up to %s before %s", target.name, reason)


def _rebuild_table(engine: Engine, table_name: str, wanted: tuple[str, ...]) -> None:
    """Recreate one table with its current constraints, preserving all rows.

    A no-op once the expected constraint is present, so it runs at most once and
    is harmless on a database created fresh.
    """
    from app.db.models import Base as _Base

    with engine.connect() as probe:
        tables = {
            row[0]
            for row in probe.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if table_name not in tables:
            return
        if set(wanted) in _unique_index_columns(probe, table_name):
            return  # Already the right shape.
        before = probe.exec_driver_sql(
            f"SELECT COUNT(*) FROM {table_name}"
        ).scalar_one()
        existing_columns = [
            row[1] for row in probe.exec_driver_sql(f"PRAGMA table_info({table_name})")
        ]
        # Index names are unique across the whole database and are NOT renamed
        # along with their table, so the old ones have to go before the new
        # table can create indexes of the same name. Auto-created constraint
        # indexes cannot be dropped explicitly; they disappear with the table.
        old_indexes = [
            row[1]
            for row in probe.exec_driver_sql(f"PRAGMA index_list({table_name})")
            if not str(row[1]).startswith("sqlite_autoindex")
        ]

    table = _Base.metadata.tables[table_name]
    shared = [c.name for c in table.columns if c.name in existing_columns]
    column_list = ", ".join(f'"{c}"' for c in shared)
    old_name = f"{table_name}__old"

    _backup_database("schema-change")
    logger.info(
        "Rebuilding %s to change its unique constraint (%d rows)", table_name, before
    )

    with engine.connect() as conn:
        # Both pragmas must be set outside a transaction, and SQLAlchemy opens
        # one implicitly on first use, so the commit clears it before BEGIN.
        #
        # legacy_alter_table keeps SQLite from helpfully rewriting other tables'
        # foreign keys to follow the rename -- which would leave them pointing
        # at the temporary table we are about to drop.
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        conn.commit()

        try:
            with conn.begin():
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table_name}" RENAME TO "{old_name}"'
                )
                for index_name in old_indexes:
                    conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{index_name}"')
                table.create(bind=conn)
                conn.exec_driver_sql(
                    f'INSERT INTO "{table_name}" ({column_list}) '
                    f'SELECT {column_list} FROM "{old_name}"'
                )
                after = conn.exec_driver_sql(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).scalar_one()
                if after != before:
                    # Never destroy the original on a partial copy; the rollback
                    # puts everything back exactly as it was.
                    raise RuntimeError(
                        f"{table_name}: copied {after} of {before} rows; aborting"
                    )
                conn.exec_driver_sql(f'DROP TABLE "{old_name}"')
        finally:
            conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()

    logger.info("Rebuilt %s, %d rows preserved", table_name, before)


def _migrate_list_mappings(engine: Engine) -> None:
    """Move the old one-collection-per-list settings into ListMapping rows.

    Runs once. Each list that pointed at a collection becomes a mapping row
    carrying the same read and write flags, so an existing setup keeps working
    exactly as it did before gaining the ability to fan out.
    """
    with engine.begin() as conn:
        already = conn.exec_driver_sql("SELECT COUNT(*) FROM list_mappings").scalar_one()
        if already:
            return
        rows = list(
            conn.exec_driver_sql(
                "SELECT id, sync_group_id, read_enabled, write_enabled "
                "FROM remote_lists WHERE sync_group_id IS NOT NULL"
            )
        )
        for list_id, group_id, read_enabled, write_enabled in rows:
            conn.exec_driver_sql(
                "INSERT INTO list_mappings "
                "(remote_list_id, sync_group_id, read_enabled, write_enabled, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (list_id, group_id, read_enabled, write_enabled),
            )
        if rows:
            logger.info("Migrated %d list mapping(s) to the new table", len(rows))


def _backfill_item_origin_lists(engine: Engine) -> None:
    """Work out which list each existing item first came from.

    New items record it as they are created, but everything already in the
    database predates the column. The item's link in its origin account is the
    best available answer, and a far better one than null -- an item with no
    known origin is treated as unrestricted, so leaving them all null would make
    every filtered destination behave as though it had no filter.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE items SET origin_remote_list_id = ("
            "  SELECT item_links.remote_list_id FROM item_links"
            "   WHERE item_links.item_id = items.id"
            "     AND item_links.account_id = items.origin_account_id"
            "   ORDER BY item_links.id LIMIT 1"
            ") WHERE origin_remote_list_id IS NULL AND origin_account_id IS NOT NULL"
        )


def _backfill_item_link_groups(engine: Engine) -> None:
    """Fill in ItemLink.sync_group_id from the item each link belongs to."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE item_links SET sync_group_id = ("
            "  SELECT items.sync_group_id FROM items WHERE items.id = item_links.item_id"
            ") WHERE sync_group_id IS NULL"
        )


def init_db() -> None:
    """Create any missing tables and columns. Safe to run on every startup."""
    ensure_directories()
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _apply_column_migrations(engine)
    for table_name, unique_columns in _CONSTRAINT_REBUILDS:
        _rebuild_table(engine, table_name, unique_columns)
    _migrate_list_mappings(engine)
    _backfill_item_link_groups(engine)
    _backfill_item_origin_lists(engine)
    _close_interrupted_runs()
    logger.info("Database ready at %s", DB_PATH)
