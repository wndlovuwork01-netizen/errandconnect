You are maintaining and extending an errand marketplace web application with two roles:
Client
Runner
Existing system already includes:
Authentication
Chat system
Errand creation
Errand acceptance
Price agreement tracking
Errand confirmation workflow
ETA-based safe tracking
chats.html and errandfinal.html
You must now implement the following NEW features:
Runner Registration System
Client Map Integration
Intelligent Minimum Fee Calculation System
Do NOT break existing functionality.

FEATURE 1: RUNNER REGISTRATION SYSTEM
Create a runner registration page and backend logic.
Template:
runner_register.html
Purpose:
Allow users to register as runners.

RUNNER DATABASE MODEL
Create RunnerProfile table:
id
user_id (foreign key)
full_name
phone_number
national_id_number
profile_photo
vehicle_type ("foot", "bike", "motorcycle", "car", "truck")
vehicle_registration_number (nullable)
is_verified (boolean default false)
is_available (boolean default true)
current_latitude (nullable)
current_longitude (nullable)
created_at

RUNNER REGISTRATION FLOW
User opens runner_register.html
User submits:
Full name
Phone number
National ID
Profile photo
Vehicle type
Vehicle registration number (optional depending on vehicle)
Backend creates RunnerProfile.
User role becomes runner-enabled.

RUNNER AVAILABILITY SYSTEM
Runner can toggle:
Available
Unavailable
This determines visibility in:
Available Runners page

FEATURE 2: CLIENT MAP INTEGRATION
Client must be able to see a map for:
Creating errands
Tracking runner ETA safely
Template affected:
create_errand.html
chats.html

CREATE ERRAND MAP REQUIREMENTS
Client must:
Select pickup location on map
Select dropoff location on map
System stores:
pickup_latitude
pickup_longitude
dropoff_latitude
dropoff_longitude
in Errand table

ERRAND TABLE ADDITIONS
pickup_latitude
pickup_longitude
dropoff_latitude
dropoff_longitude
distance_km
Distance must be auto-calculated using coordinates.

TRACKING MAP REQUIREMENTS (CLIENT SIDE)
Client must see map showing:
Pickup location
Dropoff location
IMPORTANT SAFETY RULE:
DO NOT show runner live location.
ONLY show:
Pickup point
Destination point
ETA remaining

FEATURE 3: MINIMUM FEE CALCULATION SYSTEM
System must automatically calculate minimum fee based on:
Distance
Weight
Time of day
Base fee
Vehicle type
This prevents underpricing and ensures fair compensation.

ERRAND TABLE ADDITIONS
weight_kg
calculated_minimum_fee

GLOBAL FEE CONFIGURATION TABLE
FeeConfig table:
id
base_fee
per_km_fee
per_kg_fee
night_multiplier
rush_hour_multiplier
vehicle_type_multiplier_json
Example vehicle_type_multiplier_json:
{
"foot": 1.0,
"bike": 1.2,
"motorcycle": 1.5,
"car": 2.0,
"truck": 3.0
}

MINIMUM FEE CALCULATION FUNCTION
calculate_minimum_fee(errand):
Inputs:
distance_km
weight_kg
vehicle_type
current_time
Logic:
fee = base_fee
fee += distance_km * per_km_fee
fee += weight_kg * per_kg_fee
Apply time multiplier:
If time between 18:00 and 06:00:
fee *= night_multiplier
If rush hour (07:00–09:00, 16:00–18:00):
fee *= rush_hour_multiplier
Apply vehicle multiplier:
fee *= vehicle_type_multiplier
Return fee
Store in:
errand.calculated_minimum_fee

ERRAND CREATION VALIDATION
When client creates errand:
If client_price < calculated_minimum_fee:
Reject creation
Show error:
"Price is below minimum allowed fee. Minimum fee is X"

AVAILABLE ERRANDS PAGE UPDATE
Runner must see:
Distance
Weight
Calculated minimum fee
Client offered fee
Runner can:
Accept client price
OR
Propose new price >= calculated minimum fee

AVAILABLE RUNNERS PAGE UPDATE
Client must see:
Runner vehicle type
Estimated arrival time (calculated from pickup distance)
Runner rating (if exists)

MAP DISTANCE CALCULATION FUNCTION
calculate_distance(lat1, lon1, lat2, lon2)
Use Haversine formula.
Store result in errand.distance_km

ETA CALCULATION FUNCTION
calculate_eta(distance_km, vehicle_type)
Example speeds:
foot = 5 km/h
bike = 15 km/h
motorcycle = 35 km/h
car = 50 km/h
eta_minutes = distance_km / speed * 60

CHATS.HTML UPDATE
Client sees:
ETA remaining
Errand status
Map with pickup and dropoff markers
Runner sees:
Update ETA button

SECURITY REQUIREMENTS
DO NOT expose runner real-time GPS
ONLY store and display ETA
Runner location coordinates must NOT be visible to client

NEW BACKEND FUNCTIONS REQUIRED
register_runner(user_id, data)
toggle_runner_availability(user_id, is_available)
calculate_distance(lat1, lon1, lat2, lon2)
calculate_minimum_fee(errand)
calculate_eta(distance_km, vehicle_type)
create_errand(client_id, errand_data)
update_runner_eta(errand_id, eta_minutes)

FRONTEND FILES REQUIRED
runner_register.html
create_errand.html (with map)
chats.html (map + ETA display)
errandfinal.html (agreement confirmation)

GOAL
Implement a complete errand logistics system with:
Runner registration
Map-based errand creation
Intelligent minimum fee calculation
Safe ETA tracking
Price agreement tracking
Confirmation workflow
Chat integration
WITHOUT breaking existing functionality