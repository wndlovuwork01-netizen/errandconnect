
from app import app, db
from sqlalchemy import inspect
import sqlalchemy

def sync():
    with app.app_context():
        inspector = inspect(db.engine)
        
        # Check tables
        tables = inspector.get_table_names()
        print(f"Existing tables: {tables}")
        
        # Create all missing tables
        db.create_all()
        print("Ensured all tables exist.")
        
        # Check for missing columns in runner_profiles
        if 'runner_profiles' in tables:
            columns = [c['name'] for c in inspector.get_columns('runner_profiles')]
            print(f"Columns in runner_profiles: {columns}")
            
            missing_runner_cols = {
                'full_name': sqlalchemy.String(150),
                'phone_number': sqlalchemy.String(40),
                'national_id_number': sqlalchemy.String(100),
                'profile_photo': sqlalchemy.String(300),
                'city': sqlalchemy.String(100),
                'vehicle_type': sqlalchemy.String(50),
                'vehicle_registration_number': sqlalchemy.String(100),
                'is_verified': sqlalchemy.Boolean(),
                'is_available': sqlalchemy.Boolean(),
                'current_latitude': sqlalchemy.Float(),
                'current_longitude': sqlalchemy.Float(),
            }
            
            for col_name, col_type in missing_runner_cols.items():
                if col_name not in columns:
                    print(f"Adding missing column {col_name} to runner_profiles...")
                    type_str = str(col_type)
                    if isinstance(col_type, sqlalchemy.String):
                        type_str = f"VARCHAR({col_type.length})"
                    elif isinstance(col_type, sqlalchemy.Boolean):
                        type_str = "BOOLEAN"
                    elif isinstance(col_type, sqlalchemy.Float):
                        type_str = "FLOAT"
                    
                    try:
                        db.session.execute(sqlalchemy.text(f'ALTER TABLE runner_profiles ADD COLUMN {col_name} {type_str}'))
                    except Exception as e:
                        print(f"Error adding column {col_name}: {e}")
        
        # Check for missing columns in errands
        if 'errands' in tables:
            errand_columns = [c['name'] for c in inspector.get_columns('errands')]
            print(f"Columns in errands: {errand_columns}")
            
            missing_errand_cols = {
                'pickup_latitude': sqlalchemy.Float(),
                'pickup_longitude': sqlalchemy.Float(),
                'dropoff_latitude': sqlalchemy.Float(),
                'dropoff_longitude': sqlalchemy.Float(),
                'distance_km': sqlalchemy.Float(),
                'calculated_minimum_fee': sqlalchemy.Float(),
                'weight_kg': sqlalchemy.String(50),
                'agreed_price': sqlalchemy.Float(),
            }
            
            for col_name, col_type in missing_errand_cols.items():
                if col_name not in errand_columns:
                    print(f"Adding missing column {col_name} to errands...")
                    type_str = "FLOAT"
                    if isinstance(col_type, sqlalchemy.String):
                        type_str = f"VARCHAR({col_type.length})"
                    try:
                        db.session.execute(sqlalchemy.text(f'ALTER TABLE errands ADD COLUMN {col_name} {type_str}'))
                    except Exception as e:
                        print(f"Error adding column {col_name}: {e}")

        # Check for missing columns in active_errands
        if 'active_errands' in tables:
            active_errand_columns = [c['name'] for c in inspector.get_columns('active_errands')]
            print(f"Columns in active_errands: {active_errand_columns}")
            
            missing_active_cols = {
                'estimated_duration': sqlalchemy.String(100),
            }
            
            for col_name, col_type in missing_active_cols.items():
                if col_name not in active_errand_columns:
                    print(f"Adding missing column {col_name} to active_errands...")
                    type_str = f"VARCHAR({col_type.length})"
                    try:
                        db.session.execute(sqlalchemy.text(f'ALTER TABLE active_errands ADD COLUMN {col_name} {type_str}'))
                    except Exception as e:
                        print(f"Error adding column {col_name}: {e}")

        db.session.commit()
        print("Sync complete.")

if __name__ == "__main__":
    sync()
