import sys
import os

# Add current directory to path so we can import app
sys.path.insert(0, os.getcwd())

# Import our app and db
from app import app, db
from sqlalchemy import text

def add_columns():
    with app.app_context():
        print("Adding missing columns to runner_profiles table...")
        
        # Add remaining_errands column
        try:
            db.session.execute(text("ALTER TABLE runner_profiles ADD COLUMN remaining_errands INTEGER DEFAULT 0"))
            print("✓ Column 'remaining_errands' added")
        except Exception as e:
            if 'duplicate column' in str(e).lower():
                print("• Column 'remaining_errands' already exists")
            else:
                print(f"⚠️ Error: {e}")
        
        # Add errand_deducted_ids column
        try:
            db.session.execute(text("ALTER TABLE runner_profiles ADD COLUMN errand_deducted_ids TEXT DEFAULT ''"))
            print("✓ Column 'errand_deducted_ids' added")
        except Exception as e:
            if 'duplicate column' in str(e).lower():
                print("• Column 'errand_deducted_ids' already exists")
            else:
                print(f"⚠️ Error: {e}")
        
        db.session.commit()
        print("\nDone!")

if __name__ == "__main__":
    add_columns()