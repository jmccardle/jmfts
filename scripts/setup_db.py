#!/usr/bin/env python3
"""
JMFTS Database Setup Script

Creates the database and applies the schema.
Run with: python -m scripts.setup_db
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from jmfts_core.config import get_settings


def create_database():
    """Create the JMFTS database if it doesn't exist"""
    settings = get_settings()

    # Connect to postgres database to create our database
    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    cursor = conn.cursor()

    # Check if database exists
    cursor.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s",
        (settings.db_name,)
    )
    exists = cursor.fetchone()

    if not exists:
        print(f"Creating database '{settings.db_name}'...")
        cursor.execute(f'CREATE DATABASE "{settings.db_name}"')
        print("Database created.")
    else:
        print(f"Database '{settings.db_name}' already exists.")

    cursor.close()
    conn.close()


def apply_schema():
    """Apply the SQL schema to the database"""
    settings = get_settings()

    # Read schema file
    schema_path = project_root / "schema.sql"
    if not schema_path.exists():
        print(f"Error: Schema file not found at {schema_path}")
        return False

    schema_sql = schema_path.read_text()

    # Connect to the JMFTS database
    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )

    cursor = conn.cursor()

    try:
        print("Applying schema...")
        cursor.execute(schema_sql)
        conn.commit()
        print("Schema applied successfully.")
    except psycopg2.Error as e:
        print(f"Error applying schema: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()

    return True


def test_connection():
    """Test the database connection and basic operations"""
    from jmfts_core.database import get_session
    from jmfts_core.models.document import Document

    print("\nTesting database connection...")

    try:
        with get_session() as session:
            # Try a simple query
            count = session.query(Document).count()
            print(f"Connection successful! Document count: {count}")
            return True
    except Exception as e:
        print(f"Connection test failed: {e}")
        return False


def main():
    print("=" * 60)
    print("JMFTS Database Setup")
    print("=" * 60)

    settings = get_settings()
    print(f"\nConfiguration:")
    print(f"  Host: {settings.db_host}:{settings.db_port}")
    print(f"  Database: {settings.db_name}")
    print(f"  User: {settings.db_user}")
    print()

    # Create database
    try:
        create_database()
    except Exception as e:
        print(f"Error creating database: {e}")
        print("\nMake sure PostgreSQL is running and credentials are correct.")
        return 1

    # Apply schema
    if not apply_schema():
        return 1

    # Test connection
    if not test_connection():
        return 1

    print("\n" + "=" * 60)
    print("Setup complete! You can now start the JMFTS API:")
    print("  python -m api.main")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
