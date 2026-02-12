import os
import re
import json
import math
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from config import Config
from extensions import db
from math import radians, sin, cos, sqrt, atan2

# Create instance folder if missing
os.makedirs(os.path.join(os.path.dirname(__file__), "instance"), exist_ok=True)
os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = os.environ.get("SECRET_KEY", "dev_fallback_key")

db.init_app(app)

# ============================================================================
# IMPORT MODELS
# ============================================================================
from models import User, RunnerProfile, Errand, Negotiation, ActiveErrand, Rating, Notification, AppFeedback, FeeConfig, Chat, Message

# ============================================================================
# HELPERS
# ============================================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_id') is None:
            return redirect(url_for('signin', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def calculate_minimum_fee(distance_km, weight_kg, vehicle_type, current_time):
    fee_config = FeeConfig.query.first()
    if not fee_config:
        # Default fallback
        base_fee = 5.0
        per_km = 1.5
        per_kg = 0.5
        night_mult = 1.5
        rush_mult = 1.2
        vehicle_mults = {"foot": 1.0, "bike": 1.2, "motorcycle": 1.5, "car": 2.0, "truck": 3.0}
    else:
        base_fee = fee_config.base_fee
        per_km = fee_config.per_km_fee
        per_kg = fee_config.per_kg_fee
        night_mult = fee_config.night_multiplier
        rush_mult = fee_config.rush_hour_multiplier
        vehicle_mults = json.loads(fee_config.vehicle_type_multiplier_json)

    try:
        weight = float(weight_kg)
    except:
        weight = 0

    fee = base_fee + (distance_km * per_km) + (weight * per_kg)

    # Time multipliers
    hour = current_time.hour
    if 22 <= hour or hour <= 6:
        fee *= night_mult
    elif (7 <= hour <= 9) or (16 <= hour <= 18):
        fee *= rush_mult
        
    # Vehicle multiplier
    v_mult = vehicle_mults.get(vehicle_type, 1.0)
    fee *= v_mult
    
    return round(fee, 2)

def get_available_errands_count(user_id):
    runner_profile = RunnerProfile.query.filter_by(user_id=user_id).first()
    runner_city = getattr(runner_profile, 'city', '')

    if runner_city:
        count = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).count()
    else:
        count = Errand.query.filter_by(status="pending").count()
    return count

# ============================================================================
# JINJA FILTERS
# ============================================================================

@app.template_filter('timesince')
def timesince_filter(dt):
    """
    Returns a string representing how long ago a datetime occurred.
    """
    if not dt:
        return ""
    
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return dt

    now = datetime.now()
    # Handle timezone-naive vs timezone-aware
    if dt.tzinfo is not None:
        now = datetime.now(dt.tzinfo)
        
    diff = now - dt
    
    periods = (
        (diff.days // 365, "year", "years"),
        (diff.days // 30, "month", "months"),
        (diff.days // 7, "week", "weeks"),
        (diff.days, "day", "days"),
        (diff.seconds // 3600, "hour", "hours"),
        (diff.seconds // 60, "minute", "minutes"),
        (diff.seconds, "second", "seconds"),
    )

    for count, singular, plural in periods:
        if count >= 1:
            return f"{count} {singular if count == 1 else plural} ago"

    return "just now"

# ============================================================================
# CORE ROUTES
# ============================================================================

@app.route("/")
def index():
    if current_user():
        return redirect(url_for('home_page'))
    return render_template("index.html")

@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":
        identifier = request.form.get("identifier") or request.form.get("username") or request.form.get("email")
        password = request.form.get("password")
        
        user = User.query.filter((User.email == identifier) | (User.username == identifier)).first()
        
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            if user.user_type == "runner":
                return redirect(url_for('runnerhome'))
            return redirect(url_for('home_page'))
        
        flash("Invalid credentials", "danger")
    return render_template("signin.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        first_name = request.form.get("first_name")
        last_name = request.form.get("last_name")
        fullname = request.form.get("fullname")
        username = request.form.get("username")
        email = request.form.get("email")
        phone_number = request.form.get("phone_number")
        country_code = request.form.get("country_code")
        password = request.form.get("password")
        user_type = request.form.get("user_type", "client")

        if not fullname:
            name_parts = [part for part in [first_name, last_name] if part]
            fullname = " ".join(name_parts) if name_parts else username
        if country_code and phone_number:
            phone = f"{country_code}{phone_number}"
        else:
            phone = phone_number
        
        if User.query.filter((User.email == email) | (User.username == username)).first():
            flash("User already exists", "warning")
            return redirect(url_for('signup'))
            
        user = User(
            fullname=fullname,
            username=username,
            email=email,
            phone=phone,
            password_hash=generate_password_hash(password),
            user_type=user_type
        )
        db.session.add(user)
        db.session.commit()
        
        flash("Account created! Please sign in.", "success")
        return redirect(url_for('signin'))
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.pop('user_id', None)
    return redirect(url_for('signin'))

@app.route("/home")
@login_required
def home_page():
    user = current_user()
    if user.user_type == "runner":
        return redirect(url_for('runnerhome'))
    
    # Fetch counts for client
    pending_count = Errand.query.filter_by(client_id=user.id, status="pending").count()
    completed_count = Errand.query.filter_by(client_id=user.id, status="completed").count()
    
    return render_template("home.html", user=user, pending_count=pending_count, completed_count=completed_count)

@app.route("/runnerhome")
@login_required
def runnerhome():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
        
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    if not runner_profile:
        flash("Please complete your profile", "info")
        return redirect(url_for('runner_register'))
        
    # Stats logic (simplified for restoration)
    completed = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").count()
    active = ActiveErrand.query.filter_by(runner_id=user.id, status="ongoing").count()
    
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    runner_city = getattr(runner_profile, 'city', '')

    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").all()
    
    # Calculate total earnings
    completed_errands_list = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").all()
    total_earnings = 0.0
    for active_errand in completed_errands_list:
        negotiation = Negotiation.query.filter_by(
            errand_id=active_errand.errand_id,
            runner_id=user.id,
            status="accepted"
        ).first()
        if negotiation:
            total_earnings += negotiation.offer_price
        else:
            total_earnings += active_errand.errand.price_estimate

    return render_template("runnerhome.html", 
                         user=user, 
                         completed_count=completed, 
                         pending_count=active, 
                         available_errands=available_errands,
                         total_earnings=total_earnings)

@app.route("/runnerprofile")
@login_required
def runnerprofile():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    completed_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").all()
    active_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="ongoing").all()
    completed_count = len(completed_errands)
    active_count = len(active_errands)
    total_earnings = 0.0
    for active_errand in completed_errands:
        negotiation = Negotiation.query.filter_by(
            errand_id=active_errand.errand_id,
            runner_id=user.id,
            status="accepted"
        ).first()
        if negotiation:
            total_earnings += negotiation.offer_price
        else:
            total_earnings += active_errand.errand.price_estimate
    ratings = Rating.query.filter_by(to_user_id=user.id).all()
    avg_rating = sum(r.rating for r in ratings) / len(ratings) if ratings else 0
    return render_template(
        "runnerprofile.html",
        user=user,
        runner_profile=runner_profile,
        completed_count=completed_count,
        active_count=active_count,
        total_earnings=total_earnings,
        avg_rating=avg_rating
    )

@app.route("/dashboardrunner")
@login_required
def dashboardrunner():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
    
    # Stats logic
    completed_count = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").count()
    active_count = ActiveErrand.query.filter_by(runner_id=user.id, status="ongoing").count()
    
    # Earnings logic
    today = datetime.utcnow().date()
    today_errands = ActiveErrand.query.filter(
        ActiveErrand.runner_id == user.id,
        ActiveErrand.status == "completed",
        db.func.date(ActiveErrand.end_time) == today
    ).all()
    today_earnings = sum(errand.errand.price_estimate for errand in today_errands)
    
    # Available errands
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    runner_city = getattr(runner_profile, 'city', '')
    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").all()
    
    # Notifications
    notifications = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(5).all()
    
    # Ratings
    ratings = Rating.query.filter_by(to_user_id=user.id).all()
    avg_rating = sum(r.rating for r in ratings) / len(ratings) if ratings else 0
    
    # Weekly earnings (mock data for chart)
    weekly_earnings = []
    for i in range(7):
        date = today - timedelta(days=6-i)
        day_errands = ActiveErrand.query.filter(
            ActiveErrand.runner_id == user.id,
            ActiveErrand.status == "completed",
            db.func.date(ActiveErrand.end_time) == date
        ).all()
        earnings = sum(errand.errand.price_estimate for errand in day_errands)
        weekly_earnings.append({"day": date.strftime('%a'), "earnings": earnings})

    return render_template("runnerdashboard.html", 
                         user=user, 
                         available_errands=available_errands,
                         completed_count=completed_count,
                         active_count=active_count,
                         today_earnings=today_earnings,
                         notifications=notifications,
                         weekly_earnings=weekly_earnings,
                         avg_rating=avg_rating)

@app.route("/runnercompleted")
@login_required
def runnercompleted():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
    # Fetch completed errands for this runner
    completed_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").all()
    return render_template("runnercompleted.html", user=user, completed_errands=completed_errands)

@app.route("/runneravailable_errands")
@login_required
def runneravailable_errands():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
    
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    runner_city = getattr(runner_profile, 'city', '')

    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").all()
        
    return render_template("runneravailable_errands.html", user=user, available_errands=available_errands)

@app.route("/runnerhistory")
@login_required
def runnerhistory():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
    
    # Fetch all active errands for this runner
    active_errands = ActiveErrand.query.filter_by(runner_id=user.id).order_by(ActiveErrand.id.desc()).all()
    
    total_orders = len(active_errands)
    total_amount = sum(ae.errand.price_estimate for ae in active_errands if ae.status == "completed")
    completed_orders = len([ae for ae in active_errands if ae.status == "completed"])
    
    return render_template("runnerhistory.html", 
                         user=user, 
                         orders=active_errands, 
                         total_orders=total_orders, 
                         total_amount=total_amount, 
                         completed_orders=completed_orders)

@app.route("/runnerwallet")
@login_required
def runnerwallet():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
    
    # Simplified wallet logic
    completed_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").all()
    total_balance = sum(errand.errand.price_estimate for errand in completed_errands)
    available_balance = total_balance # Simplified
    pending_balance = 0.0
    
    # Transactions would normally come from a Transaction model, which doesn't seem to exist yet
    # We can pass an empty list or mock some from completed errands
    transactions = [] 
    
    return render_template("runnerwallet.html", 
                         user=user, 
                         total_balance=total_balance, 
                         available_balance=available_balance, 
                         pending_balance=pending_balance,
                         transactions=transactions)

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user.user_type == "runner":
        return redirect(url_for('runnerhome'))
    return render_template("dashboard_client.html", user=user)

@app.route("/order_history")
@login_required
def order_history():
    user = current_user()
    # Fetch orders for this user
    orders = Errand.query.filter_by(client_id=user.id).order_by(Errand.created_at.desc()).all()
    
    # Calculate counts for stats
    total_orders = len(orders)
    pending_count = Errand.query.filter_by(client_id=user.id, status="pending").count()
    completed_count = Errand.query.filter_by(client_id=user.id, status="completed").count()
    
    return render_template("order_history.html", 
                         user=user, 
                         orders=orders, 
                         total_orders=total_orders,
                         pending_count=pending_count,
                         completed_count=completed_count,
                         now=datetime.utcnow())

@app.route("/settings")
@login_required
def settings():
    user = current_user()
    return render_template("settings.html", user=user)

@app.route("/map_view")
@login_required
def map_view():
    user = current_user()
    return render_template("map_view.html", user=user)

@app.route("/notifications")
@login_required
def notifications():
    user = current_user()
    notifications_list = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).all()
    return render_template("notifications.html", user=user, notifications=notifications_list)

@app.route("/ratings")
@login_required
def ratings():
    user = current_user()
    ratings_list = Rating.query.filter_by(to_user_id=user.id).all()
    return render_template("ratings.html", user=user, ratings=ratings_list)

@app.route("/profile")
@login_required
def profile():
    user = current_user()
    return render_template("profile.html", user=user)

@app.route("/wallet")
@login_required
def wallet():
    user = current_user()
    return render_template("wallet.html", user=user)

@app.route("/completed")
@login_required
def completed():
    user = current_user()
    return render_template("completed.html", user=user)

@app.route("/terms")
@login_required
def terms():
    user = current_user()
    return render_template("terms.html", user=user, current_date=datetime.utcnow())

@app.route("/privacy")
@login_required
def privacy():
    user = current_user()
    return render_template("Privacy.html", user=user)

@app.route("/help")
@login_required
def help_support():
    user = current_user()
    return render_template("help.html", user=user)

@app.route("/personal_info")
@login_required
def personal_info():
    user = current_user()
    return render_template("personal_info.html", user=user)

@app.route("/privacy_security")
@login_required
def privacy_security():
    user = current_user()
    return render_template("Privacy.html", user=user)

@app.route("/rate_app")
@login_required
def rate_app():
    user = current_user()
    return render_template("rate.html", user=user)

# ============================================================================
# ERRAND ROUTES
# ============================================================================

@app.route("/create_grocery_errand", methods=["GET", "POST"])
@login_required
def create_grocery_errand():
    user = current_user()
    if request.method == "POST":
        # Simplified creation logic similar to create_errand
        return redirect(url_for('home_page'))
    return render_template("grocery.html", user=user)

@app.route("/create_food_delivery_errand", methods=["GET", "POST"])
@login_required
def create_food_delivery_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("food_delivery.html", user=user)

@app.route("/create_bill_payment_errand", methods=["GET", "POST"])
@login_required
def create_bill_payment_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("bill_payments.html", user=user)

@app.route("/create_package_delivery_errand", methods=["GET", "POST"])
@login_required
def create_package_delivery_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("package_delivery.html", user=user)

@app.route("/create_gadget_service_errand", methods=["GET", "POST"])
@login_required
def create_gadget_service_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("gadget_service.html", user=user)

@app.route("/create_collections_errand", methods=["GET", "POST"])
@login_required
def create_collections_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("Collections.html", user=user)

@app.route("/create_ticket_booking_errand", methods=["GET", "POST"])
@login_required
def create_ticket_booking_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("ticket_booking.html", user=user)

@app.route("/create_spare_parts_errand", methods=["GET", "POST"])
@login_required
def create_spare_parts_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("spare_parts.html", user=user)

@app.route("/create_gas_delivery_errand", methods=["GET", "POST"])
@login_required
def create_gas_delivery_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("gas_delivery.html", user=user)

@app.route("/create_other_service_errand", methods=["GET", "POST"])
@login_required
def create_other_service_errand():
    user = current_user()
    if request.method == "POST":
        return redirect(url_for('home_page'))
    return render_template("other.html", user=user)

@app.route("/purchase_page")
@login_required
def purchase_page():
    user = current_user()
    return render_template("purchase.html", user=user)

@app.route("/property_page")
@login_required
def property_page():
    user = current_user()
    return render_template("property.html", user=user)

@app.route("/create_errand", methods=["GET", "POST"])
@login_required
def create_errand():
    user = current_user()
    if request.method == "POST":
        pickup = request.form.get("pickup_location")
        dropoff = request.form.get("delivery_location")
        details = request.form.get("details")
        vehicle_type = request.form.get("vehicle_type", "car")
        weight = request.form.get("weight", "0")
        
        # Coordinates (hidden fields from map)
        pickup_lat = request.form.get("pickup_lat")
        pickup_lon = request.form.get("pickup_lon")
        dropoff_lat = request.form.get("dropoff_lat")
        dropoff_lon = request.form.get("dropoff_lon")
        
        distance = 0
        if pickup_lat and dropoff_lat:
            distance = calculate_distance(float(pickup_lat), float(pickup_lon), float(dropoff_lat), float(dropoff_lon))
            
        fee = calculate_minimum_fee(distance, weight, vehicle_type, datetime.now())
        
        errand = Errand(
            client_id=user.id,
            type="General",
            pickup_location=pickup,
            delivery_location=dropoff,
            pickup_latitude=float(pickup_lat) if pickup_lat else None,
            pickup_longitude=float(pickup_lon) if pickup_lon else None,
            dropoff_latitude=float(dropoff_lat) if dropoff_lat else None,
            dropoff_longitude=float(dropoff_lon) if dropoff_lon else None,
            distance_km=distance,
            weight_kg=weight,
            details=details,
            price_estimate=fee,
            calculated_minimum_fee=fee,
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()
        
        return redirect(url_for('available_runners', errand_id=errand.id))
        
    return render_template("create_errand.html", user=user)

@app.route("/runner_register", methods=["GET", "POST"])
@login_required
def runner_register():
    user = current_user()
    if request.method == "POST":
        # Handle file uploads
        profile_photo = request.files.get("profile_photo")
        filename = None
        if profile_photo and secure_filename(profile_photo.filename):
            filename = secure_filename(profile_photo.filename)
            profile_photo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            
        full_name = request.form.get("full_name")
        phone_number = request.form.get("phone_number")
        if full_name:
            user.fullname = full_name
        if phone_number:
            user.phone = phone_number

        profile = RunnerProfile(
            user_id=user.id,
            full_name=full_name,
            phone_number=phone_number,
            national_id_number=request.form.get("national_id_number"),
            vehicle_type=request.form.get("vehicle_type"),
            vehicle_registration_number=request.form.get("vehicle_registration_number"),
            profile_photo=filename,
            city=request.form.get("city", "Harare"), # Default for now
            is_available=True
        )
        db.session.add(profile)
        
        user.user_type = "runner"
        db.session.commit()
        
        flash("Registration successful!", "success")
        return redirect(url_for('runnerhome'))
        
    return render_template("runner_register.html", user=user)

@app.route("/available_runners/<int:errand_id>")
@login_required
def available_runners(errand_id):
    user = current_user()
    errand = Errand.query.get_or_404(errand_id)
    
    # Simple matching logic: all available runners
    runners = RunnerProfile.query.filter_by(is_available=True).all()
    
    return render_template("available_runners.html", user=user, errand=errand, runners=runners)

# ============================================================
# CHAT & TRACKING ROUTES (NEW)
# ============================================================

@app.route("/chats")
@login_required
def chats():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
        
    # Get all chats for this user
    if user.user_type == "client":
        user_chats = Chat.query.filter_by(client_id=user.id).order_by(Chat.created_at.desc()).all()
    else:
        user_chats = Chat.query.filter_by(runner_id=user.id).order_by(Chat.created_at.desc()).all()
        
    return render_template("chats.html", user=user, chats=user_chats)

@app.route("/chat/<int:chat_id>")
@login_required
def chat_detail(chat_id):
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
        
    chat = Chat.query.get_or_404(chat_id)
    
    # Verify user is part of this chat
    if user.id != chat.client_id and user.id != chat.runner_id:
        flash("Unauthorized access", "danger")
        return redirect(url_for("home_page"))
        
    # Mark unread messages as read
    unread_msgs = Message.query.filter_by(chat_id=chat.id, is_read=False).all()
    for msg in unread_msgs:
        if msg.sender_id != user.id:
            msg.is_read = True
    db.session.commit()
    
    messages = Message.query.filter_by(chat_id=chat.id).order_by(Message.created_at.asc()).all()
    
    # Get active errand status for tracking
    active_errand = ActiveErrand.query.filter_by(errand_id=chat.errand_id).first()
    
    return render_template("chats.html", user=user, active_chat=chat, messages=messages, active_errand=active_errand)

@app.route("/api/send_message", methods=["POST"])
@login_required
def send_message():
    user = current_user()
    data = request.json
    
    chat_id = data.get("chat_id")
    content = data.get("content")
    
    if not chat_id or not content:
        return jsonify({"error": "Missing data"}), 400
        
    chat = Chat.query.get_or_404(chat_id)
    
    if user.id != chat.client_id and user.id != chat.runner_id:
        return jsonify({"error": "Unauthorized"}), 403
        
    message = Message(
        chat_id=chat.id,
        sender_id=user.id,
        content=content
    )
    db.session.add(message)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": {
            "id": message.id,
            "content": message.content,
            "sender_id": message.sender_id,
            "created_at": message.created_at.strftime("%H:%M")
        }
    })

@app.route("/api/update_tracking", methods=["POST"])
@login_required
def update_tracking():
    user = current_user()
    if user.user_type != "runner":
        return jsonify({"error": "Only runners can update tracking"}), 403
        
    data = request.json
    errand_id = data.get("errand_id")
    duration = data.get("duration")
    
    active_errand = ActiveErrand.query.filter_by(errand_id=errand_id, runner_id=user.id).first()
    if not active_errand:
        return jsonify({"error": "Active errand not found"}), 404
        
    active_errand.estimated_duration = duration
    db.session.commit()
    
    return jsonify({"success": True})

@app.route("/errandfinal/<int:errand_id>")
@login_required
def errandfinal(errand_id):
    user = current_user()
    errand = Errand.query.get_or_404(errand_id)
    
    # Verify user is involved
    if user.id != errand.client_id:
        # Check if user is the runner involved in negotiation
        negotiation = Negotiation.query.filter_by(errand_id=errand.id, runner_id=user.id).first()
        if not negotiation:
             flash("Unauthorized access", "danger")
             return redirect(url_for("home_page"))
    
    # Logic to determine agreed price
    # If negotiation accepted, use offer_price
    # If no negotiation but runner accepted, use price_estimate
    
    accepted_negotiation = Negotiation.query.filter_by(errand_id=errand.id, status="accepted").first()
    
    if accepted_negotiation:
        agreed_price = accepted_negotiation.offer_price
        runner = User.query.get(accepted_negotiation.runner_id)
    else:
        # Check if a runner has accepted the errand without negotiation (if applicable)
        # For now assume agreed price is the estimate if no negotiation
        agreed_price = errand.price_estimate
        runner = None # Should be determined by who accepted it
        
        # If there's an active errand, we know the runner
        active = ActiveErrand.query.filter_by(errand_id=errand.id).first()
        if active:
            runner = User.query.get(active.runner_id)

    return render_template("Errandfinal.html", user=user, errand=errand, agreed_price=agreed_price, runner=runner)

@app.route("/confirm_errand_start/<int:errand_id>", methods=["POST"])
@login_required
def confirm_errand_start(errand_id):
    user = current_user()
    errand = Errand.query.get_or_404(errand_id)
    
    # If client confirms, create chat and active errand if not exists
    if user.id == errand.client_id:
        # Find the runner (passed in form or determined by negotiation)
        runner_id = request.form.get("runner_id")
        if not runner_id:
            flash("Runner not specified", "danger")
            return redirect(url_for("errandfinal", errand_id=errand.id))
            
        # Create Active Errand
        existing_active = ActiveErrand.query.filter_by(errand_id=errand.id).first()
        if not existing_active:
            active_errand = ActiveErrand(
                errand_id=errand.id,
                runner_id=runner_id,
                start_time=datetime.utcnow(),
                status="ongoing"
            )
            db.session.add(active_errand)
            
        # Create Chat
        existing_chat = Chat.query.filter_by(errand_id=errand.id).first()
        if not existing_chat:
            chat = Chat(
                errand_id=errand.id,
                client_id=user.id,
                runner_id=runner_id
            )
            db.session.add(chat)
            
        errand.status = "accepted"
        errand.agreed_price = float(request.form.get("agreed_price", errand.price_estimate))
        
        db.session.commit()
        
        # Get the chat id to redirect
        chat = Chat.query.filter_by(errand_id=errand.id).first()
        return redirect(url_for("chat_detail", chat_id=chat.id))
        
    return redirect(url_for("home_page"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
