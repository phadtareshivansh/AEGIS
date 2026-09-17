import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()

DEFAULT_DATABASE_URL = "postgresql+asyncpg://aegis:aegis@localhost:5432/aegis"


def database_url() -> str:
    """Resolve DATABASE_URL, normalizing PSQL-style schemes to asyncpg.

    Render's ``fromDatabase`` connection string is ``postgres://...`` (psycopg-
    style); we need ``postgresql+asyncpg://`` for the async driver.
    """
    url = os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(database_url(), pool_pre_ping=True)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session():
    async with async_session() as session:
        yield session