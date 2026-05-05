from app import app, db
from sqlalchemy import text

def add_missing_columns():
    with app.app_context():
        # Add expires_at column if missing
        try:
            db.session.execute(text("ALTER TABLE errands ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"))
            print("✅ expires_at column added (or already exists)")
        except Exception as e:
            print(f"Error adding expires_at: {e}")

        # Add hard_deadline column if missing
        try:
            db.session.execute(text("ALTER TABLE errands ADD COLUMN IF NOT EXISTS hard_deadline TIMESTAMP"))
            print("✅ hard_deadline column added (or already exists)")
        except Exception as e:
            print(f"Error adding hard_deadline: {e}")

        # Add distance_km column if missing
        try:
            db.session.execute(text("ALTER TABLE errands ADD COLUMN IF NOT EXISTS distance_km FLOAT"))
            print("✅ distance_km column added (or already exists)")
        except Exception as e:
            print(f"Error adding distance_km: {e}")

        db.session.commit()
        print("\n🎉 Done! All columns added successfully.")

if __name__ == "__main__":
    add_missing_columns()