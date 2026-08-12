#!/usr/bin/env python3
"""
Seed initial data into PostgreSQL database.
Creates default roles and admin user.

Usage:
  DATABASE_URL="postgresql://user:pass@host/db" python scripts/seed.py
"""

import os
import sys

# Add services to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth-service"))

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

# Import auth service models
try:
    from app.models import Role, User, UserRole
except ImportError:
    print("ERROR: Could not import auth service models. Make sure auth-service is in PYTHONPATH")
    sys.exit(1)


def get_database_url():
    """Get database URL from environment or use default."""
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL environment variable not set")
        sys.exit(1)
    return url


def seed_roles(session):
    """Create default roles."""
    roles_data = [
        {"name": "admin", "description": "Full access to all features"},
        {"name": "hr_manager", "description": "Manage HR knowledge base"},
        {"name": "hr_user", "description": "Query HR knowledge base"},
        {"name": "cc_manager", "description": "Manage contact center knowledge base"},
        {"name": "cc_user", "description": "Query contact center knowledge base"},
    ]

    created = []
    for role_data in roles_data:
        try:
            role = Role(**role_data)
            session.add(role)
            created.append(role_data["name"])
        except IntegrityError:
            session.rollback()
            print(f"  ⚠ Role '{role_data['name']}' already exists")

    if created:
        session.commit()
        print(f"✓ Created roles: {', '.join(created)}")
    else:
        print("✓ All roles already exist")


def seed_admin_user(session):
    """Create default admin user."""
    from werkzeug.security import generate_password_hash

    admin_data = {
        "username": "admin",
        "email": "admin@localhost",
        "password_hash": generate_password_hash("admin123"),  # Change in production!
        "full_name": "Administrator",
        "is_active": True,
    }

    # Check if admin exists
    admin = session.query(User).filter_by(username="admin").first()
    if admin:
        print("✓ Admin user already exists (admin / admin123)")
        return admin

    try:
        user = User(**admin_data)
        session.add(user)
        session.flush()

        # Assign admin role
        admin_role = session.query(Role).filter_by(name="admin").first()
        if admin_role:
            user_role = UserRole(user_id=user.id, role_id=admin_role.id)
            session.add(user_role)

        session.commit()
        print("✓ Created admin user: admin / admin123")
        return user
    except IntegrityError:
        session.rollback()
        print("✓ Admin user already exists")
        return None


def main():
    """Run seed script."""
    db_url = get_database_url()
    print(f"Connecting to database: {db_url.split('@')[1] if '@' in db_url else 'unknown'}")

    try:
        engine = create_engine(db_url, echo=False)
        session_local = sessionmaker(bind=engine)
        session = session_local()

        print("\n=== Seeding Database ===\n")

        # Create tables first (in case they don't exist)
        print("Creating tables (if needed)...")
        from app.models import Base

        Base.metadata.create_all(bind=engine)
        print("✓ Tables ready\n")

        # Seed roles
        print("Seeding roles...")
        seed_roles(session)
        print()

        # Seed admin user
        print("Seeding admin user...")
        seed_admin_user(session)
        print()

        print("=== Seed Complete ===\n")
        print("Admin user credentials:")
        print("  Username: admin")
        print("  Password: admin123")
        print("\n⚠️  IMPORTANT: Change admin password in production!")

        session.close()

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
