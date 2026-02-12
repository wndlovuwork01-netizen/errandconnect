import json
from app import app, db
from models import FeeConfig

def seed_fee_config():
    with app.app_context():
        if FeeConfig.query.first() is None:
            vehicle_multipliers = {
                "foot": 1.0,
                "bike": 1.2,
                "motorcycle": 1.5,
                "car": 2.0,
                "truck": 3.0
            }
            
            fee_config = FeeConfig(
                base_fee=5.0,
                per_km_fee=1.5,
                per_kg_fee=0.5,
                night_multiplier=1.5,
                rush_hour_multiplier=1.2,
                vehicle_type_multiplier_json=json.dumps(vehicle_multipliers)
            )
            db.session.add(fee_config)
            db.session.commit()
            print("FeeConfig seeded successfully.")
        else:
            print("FeeConfig already exists.")

if __name__ == "__main__":
    seed_fee_config()
