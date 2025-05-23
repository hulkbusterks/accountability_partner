# app/database.py (assuming your new structure is app/database.py)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Ensure the database file is placed in a suitable location, e.g., the project root
DATABASE_FILE = os.path.join(os.path.dirname(BASE_DIR), "sqlite.db") # Go up one level from app/

DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_FILE}"

engine = create_async_engine(DATABASE_URL, echo=True)

AsyncSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def create_tables():
    async with engine.begin() as conn:
        # Import Base from app.models (since we're in app/database.py)
        from app.models import Base
        await conn.run_sync(Base.metadata.create_all)