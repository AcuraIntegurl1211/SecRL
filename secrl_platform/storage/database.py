from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.storage.orm import Base


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_engine_and_session(
    database_path: Path,
    *,
    create: bool = False,
) -> sessionmaker[Session]:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    if create:
        Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def engine_for(session_factory: sessionmaker[Session]) -> Engine:
    engine = session_factory.kw.get("bind")
    if not isinstance(engine, Engine):
        raise TypeError("session factory is not bound to an Engine")
    return engine
