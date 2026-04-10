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
from sqlalchemy import func

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


def _extract_first(form, keys, default=""):
    for key in keys:
        value = form.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _build_service_errand(user, service_type):
    form = request.form

    pickup = _extract_first(
        form,
        [
            "pickup_location",
            "pickup_address",
            "store_location",
            "collection_location",
            "collection_address",
            "location",
        ],
        default="Pickup location not specified",
    )
    dropoff = _extract_first(
        form,
        [
            "delivery_location",
            "delivery_address",
            "dropoff_location",
            "destination",
            "address",
        ],
        default="Delivery location not specified",
    )
    vehicle_type = _extract_first(form, ["vehicle_type"], default="car")
    weight = _extract_first(form, ["weight", "weight_kg", "estimated_weight"], default="0")
    delivery_time = _extract_first(form, ["delivery_time", "specific_time"], default="")

    pickup_lat = form.get("pickup_lat")
    pickup_lon = form.get("pickup_lon")
    dropoff_lat = form.get("dropoff_lat")
    dropoff_lon = form.get("dropoff_lon")

    distance = 0
    if pickup_lat and pickup_lon and dropoff_lat and dropoff_lon:
        try:
            distance = calculate_distance(
                float(pickup_lat), float(pickup_lon), float(dropoff_lat), float(dropoff_lon)
            )
        except (TypeError, ValueError):
            distance = 0

    calculated_fee = calculate_minimum_fee(distance, weight, vehicle_type, datetime.now())
    requested_price = _extract_first(form, ["service_price", "budget_limit"], default="")
    try:
        price_estimate = float(requested_price) if requested_price else float(calculated_fee)
    except (TypeError, ValueError):
        price_estimate = float(calculated_fee)

    # Keep a compact snapshot of submitted form data for service-specific details.
    detail_pairs = []
    for key, values in form.to_dict(flat=False).items():
        if key in {"pickup_lat", "pickup_lon", "dropoff_lat", "dropoff_lon"}:
            continue
        clean_values = [str(v).strip() for v in values if str(v).strip()]
        if not clean_values:
            continue
        detail_pairs.append(f"{key}: {', '.join(clean_values)}")
    details = " | ".join(detail_pairs[:25]) if detail_pairs else f"{service_type} errand"

    errand = Errand(
        client_id=user.id,
        type=service_type,
        pickup_location=pickup,
        delivery_location=dropoff,
        weight=weight,
        delivery_time=delivery_time,
        details=details,
        price_estimate=price_estimate,
        calculated_minimum_fee=calculated_fee,
        status="pending",
    )
    db.session.add(errand)
    db.session.commit()
    return errand


@app.template_filter("timesince")
def timesince(value):
    if not value:
        return "Recently"
    now = datetime.utcnow()
    diff = now - value
    if diff.days > 0:
        return f"{diff.days}d ago"
    hours = diff.seconds // 3600
    if hours > 0:
        return f"{hours}h ago"
    minutes = diff.seconds // 60
    if minutes > 0:
        return f"{minutes}m ago"
    return "Just now"

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
        identifier = (
                request.form.get("identifier")
                or request.form.get("email")
                or request.form.get("username")
                or ""
        ).strip()
        # Do not trim password; spaces may be intentional.
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.email.ilike(identifier)) | (User.username.ilike(identifier))
        ).first()

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
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        fullname = f"{first_name} {last_name}".strip()
        email = (request.form.get("email") or "").strip().lower()
        country_code = (request.form.get("country_code") or "").strip()
        phone_number = (request.form.get("phone_number") or "").strip()
        phone = f"{country_code}{phone_number}".strip()
        date_of_birth = request.form.get("date_of_birth") # Not directly used in User model yet
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        user_type = request.form.get("user_type", "client")

        if not email or not username or not password:
            flash("Email, username and password are required", "warning")
            return redirect(url_for('signup'))
        
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
@app.route("/privacy")
def privacy():
    return render_template("privacy.html")
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
    return render_template("home.html", user=user)


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user.user_type == "runner":
        return redirect(url_for("runnerhome"))
    return render_template("dashboard_client.html", user=user)


@app.route("/profile")
@login_required
def profile():
    user = current_user()
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    return render_template("profile.html", user=user, runner_profile=runner_profile)


@app.route("/personal-info")
@login_required
def personal_info():
    user = current_user()
    return render_template("personal_info.html", user=user)


@app.route("/settings")
@login_required
def settings():
    user = current_user()
    return render_template("settings.html", user=user)


@app.route("/privacy-security")
@login_required
def privacy_security():
    user = current_user()
    return render_template("privacy.html", user=user)


@app.route("/help-support")
@login_required
def help_support():
    user = current_user()
    return render_template("help.html", user=user)


@app.route("/notifications")
@login_required
def notifications():
    user = current_user()
    items = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).all()
    return render_template("notifications.html", user=user, notifications=items)


@app.route("/rate-app", methods=["GET", "POST"])
@login_required
def rate_app():
    user = current_user()
    if request.method == "POST":
        rating = request.form.get("rating", "0")
        try:
            rating_value = max(1, min(5, int(rating)))
        except ValueError:
            rating_value = 5
        feedback = AppFeedback(
            user_id=user.id,
            rating=rating_value,
            feedback_type=request.form.get("feedback_type", "general"),
            feedback=request.form.get("feedback", "").strip() or "No feedback text provided.",
            suggestions=request.form.get("suggestions", "").strip() or None,
            contact_permission=bool(request.form.get("contact_permission")),
        )
        db.session.add(feedback)
        db.session.commit()
        flash("Thanks for your feedback.", "success")
        return redirect(url_for("settings"))
    return render_template("rate.html", user=user)

@app.route("/runnerhome")
@login_required
def runnerhome():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for('home_page'))
        
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    if not runner_profile:
        flash("Please complete your profile", "info")
        return redirect(url_for('runner_signup'))
        
    # Stats logic (simplified for restoration)
    completed = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").count()
    active = ActiveErrand.query.filter_by(runner_id=user.id, status="ongoing").count()
    available = get_available_errands_count(user.id)
    
    return render_template("runnerhome.html", user=user, completed_count=completed, active_count=active, available_count=available)

@app.route("/dashboardrunner")
@login_required
def dashboardrunner():
    return redirect(url_for('runnerhome'))

@app.route("/terms")
def terms():
    return render_template("terms.html")

# ============================================================================
# ERRAND ROUTES
# ============================================================================

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
            weight=weight,
            details=details,
            price_estimate=fee,
            calculated_minimum_fee=fee,
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()
        
        return redirect(url_for('available_runners', errand_id=errand.id))
        
    return render_template("create_errand.html", user=user)


@app.route("/order_history")
@login_required
def order_history():
    user = current_user()
    user_errands = (
        Errand.query.filter_by(client_id=user.id)
        .order_by(Errand.created_at.desc())
        .all()
    )
    total_orders = len(user_errands)
    pending_count = sum(1 for e in user_errands if e.status == "pending")
    completed_count = sum(1 for e in user_errands if e.status == "completed")
    can_delete = False
    return render_template(
        "order_history.html",
        user=user,
        errands=user_errands,
        total_orders=total_orders,
        pending_count=pending_count,
        completed_count=completed_count,
        can_delete=can_delete,
    )


@app.route("/wallet")
@login_required
def wallet():
    user = current_user()
    return render_template("wallet.html", user=user)


@app.route("/client/completed")
@login_required
def completed():
    user = current_user()
    completed_errands = ActiveErrand.query.join(Errand).filter(
        Errand.client_id == user.id,
        ActiveErrand.status == "completed"
    ).order_by(ActiveErrand.created_at.desc()).all()

    ratings = Rating.query.join(Errand).filter(Errand.client_id == user.id).all()
    avg = round(sum(r.rating for r in ratings) / len(ratings), 1) if ratings else 0
    user_rating = {"found": False}
    runner = None
    return render_template(
        "completed.html",
        user=user,
        completed_errands=completed_errands,
        average_rating=avg,
        user_rating=user_rating,
        runner=runner,
    )


@app.route("/purchase")
@login_required
def purchase_page():
    return render_template("purchase.html", user=current_user())


@app.route("/property")
@login_required
def property_page():
    return render_template("property.html", user=current_user())


@app.route("/service/grocery", methods=["GET", "POST"])
@login_required
def create_grocery_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Grocery Shopping")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("grocery.html", user=user)


@app.route("/service/food-delivery", methods=["GET", "POST"])
@login_required
def create_food_delivery_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Food Delivery")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("food_delivery.html", user=user)


@app.route("/service/bill-payment", methods=["GET", "POST"])
@login_required
def create_bill_payment_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Bill Payment")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("bill_payments.html", user=user)


@app.route("/service/package-delivery", methods=["GET", "POST"])
@login_required
def create_package_delivery_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Package Delivery")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("package_delivery.html", user=user)


@app.route("/service/gadget-service", methods=["GET", "POST"])
@login_required
def create_gadget_service_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Gadget Service")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("gadget_service.html", user=user)


@app.route("/service/collections", methods=["GET", "POST"])
@login_required
def create_collections_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Collections")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("Collections.html", user=user)


@app.route("/service/ticket-booking", methods=["GET", "POST"])
@login_required
def create_ticket_booking_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Ticket Booking")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("ticket_booking.html", user=user)


@app.route("/service/spare-parts", methods=["GET", "POST"])
@login_required
def create_spare_parts_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Spare Parts")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("spare_parts.html", user=user)


@app.route("/service/gas-delivery", methods=["GET", "POST"])
@login_required
def create_gas_delivery_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Gas Delivery")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("gas_delivery.html", user=user)


@app.route("/service/other", methods=["GET", "POST"])
@login_required
def create_other_service_errand():
    user = current_user()
    if request.method == "POST":
        errand = _build_service_errand(user, "Other Service")
        return redirect(url_for("available_runners", errand_id=errand.id))
    return render_template("other.html", user=user)


@app.route("/service/purchase", methods=["POST"])
@login_required
def create_purchase_errand():
    errand = _build_service_errand(current_user(), "General Purchasing")
    return redirect(url_for("available_runners", errand_id=errand.id))


@app.route("/service/property", methods=["POST"])
@login_required
def create_property_errand():
    errand = _build_service_errand(current_user(), "Property Purchase")
    return redirect(url_for("available_runners", errand_id=errand.id))

@app.route("/runner_signup", methods=["GET", "POST"])
@login_required
def runner_signup():
    user = current_user()
    if request.method == "POST":
        # Handle file uploads
        profile_photo = request.files.get("profile_photo")
        filename = None
        if profile_photo and secure_filename(profile_photo.filename):
            filename = secure_filename(profile_photo.filename)
            profile_photo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            
        profile = RunnerProfile(
            user_id=user.id,
            full_name=request.form.get("full_name"),
            phone_number=request.form.get("phone_number"),
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
        
    return render_template("runner_signup.html", user=user)

@app.route('/runner/profile')
def runnerprofile():
    user_id = session.get('user_id')
    if not user_id:
        return "Not logged in", 401
    user = User.query.get(user_id)
    completed_count = db.session.query(func.count(Errand.id))\
        .join(ActiveErrand, ActiveErrand.errand_id == Errand.id)\
        .filter(
            ActiveErrand.runner_id == user_id,
            Errand.status == 'completed'
        ).scalar()
    active_count = db.session.query(func.count(Errand.id))\
        .join(ActiveErrand, ActiveErrand.errand_id == Errand.id)\
        .filter(
            ActiveErrand.runner_id == user_id,
            Errand.status == 'accepted'
        ).scalar()
    total_earnings = db.session.query(
        func.coalesce(func.sum(Errand.agreed_price), 0)
    ).join(ActiveErrand, ActiveErrand.errand_id == Errand.id)\
     .filter(
        ActiveErrand.runner_id == user_id,
        Errand.status == 'completed'
    ).scalar()
    avg_rating = db.session.query(
        func.coalesce(func.avg(Rating.rating), 0)
    ).join(Errand, Rating.errand_id == Errand.id)\
     .join(ActiveErrand, ActiveErrand.errand_id == Errand.id)\
     .filter(ActiveErrand.runner_id == user_id)\
     .scalar()
    runner_profile = RunnerProfile.query.filter_by(user_id=user_id).first()
    return render_template(
        "runnerprofile.html",
        user=user,
        runner_profile=runner_profile,
        completed_count=completed_count,
        active_count=active_count,
        avg_rating=avg_rating,
        total_earnings=total_earnings
    )

@app.route("/runner/completed")
@login_required
def runnercompleted():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for("home_page"))
    completed_errands = (
        db.session.query(ActiveErrand)
        .join(Errand, ActiveErrand.errand_id == Errand.id)
        .filter(
            ActiveErrand.runner_id == user.id,
            ActiveErrand.status == "completed"
        )
        .all()
    )
    total_completed = len(completed_errands)
    total_earnings = sum(
        e.errand.agreed_price or e.errand.price_estimate or 0
        for e in completed_errands
    )
    ratings = (
        db.session.query(Rating.rating)
        .join(Errand, Rating.errand_id == Errand.id)
        .join(ActiveErrand, ActiveErrand.errand_id == Errand.id)
        .filter(ActiveErrand.runner_id == user.id)
        .all()
    )
    rating_values = [r[0] for r in ratings if r[0] is not None]
    average_rating = round(sum(rating_values) / len(rating_values), 1) if rating_values else 0
    return render_template(
        "runnercompleted.html",
        user=user,
        completed_errands=completed_errands,
        total_completed=total_completed,
        total_earnings=total_earnings,
        average_rating=average_rating
    )

@app.route("/runner/available-errands")
@login_required
def runneravailable_errands():
    user = current_user()
    if user.user_type != "runner":
        return redirect(url_for("home_page"))
    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    runner_city = getattr(runner_profile, "city", "")
    if runner_city:
        errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).all()
    else:
        errands = Errand.query.filter_by(status="pending").all()
    clean_errands = []
    for e in errands:
        clean_errands.append({
            "id": int(e.id) if e.id else 0,
            "type": e.type or "",
            "pickup_location": e.pickup_location or "",
            "delivery_location": e.delivery_location or "",
            "price": float(e.agreed_price if e.agreed_price is not None else (e.price_estimate or 0)),
            "status": e.status or "pending"
        })
    return render_template(
        "runneravailable_errands.html",
        user=user,
        errands=clean_errands
    )

@app.route("/runner/history")
@login_required
def runnerhistory():
    user = current_user()

    if user.user_type != "runner":
        return redirect(url_for("home_page"))

    # Get ALL errands linked to this runner (completed + ongoing)
    errands = ActiveErrand.query.filter_by(
        runner_id=user.id
    ).order_by(ActiveErrand.start_time.desc()).all()

    return render_template(
        "runnerhistory.html",
        user=user,
        errands=errands
    )

@app.route('/runner/wallet')
@login_required
def runnerwallet():
    user = current_user()
    return render_template("runnerwallet.html", user=user)

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


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "service": "errandconnect",
        "timestamp": datetime.utcnow().isoformat() + "Z"
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
