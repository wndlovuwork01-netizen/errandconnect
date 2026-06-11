import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), 'errandgo.db')

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Check if column already exists
    cursor.execute("PRAGMA table_info(active_errands)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'runner_marked_complete' not in columns:
        print("Adding column runner_marked_complete...")
        cursor.execute("ALTER TABLE active_errands ADD COLUMN runner_marked_complete BOOLEAN DEFAULT 0")
        conn.commit()
        print("Column added successfully!")
    else:
        print("Column already exists!")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
