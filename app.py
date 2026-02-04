import os
import re
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from config import Config
from extensions import db

# Create instance folder if missing
os.makedirs(os.path.join(os.path.dirname(__file__), "instance"), exist_ok=True)
os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = os.environ.get("SECRET_KEY", "dev_fallback_key")

db.init_app(app)


# ============================================================================
# DECORATORS & HELPERS
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


def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def validate_phone(phone):
    """
    Validate Zimbabwe mobile numbers specifically.
    Zimbabwe mobile numbers are 9 digits (excluding country code +263).
    Valid prefixes: 71, 73, 77, 78
    """
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone)

    # Check if we have exactly 9 digits (Zimbabwe mobile number format)
    if len(digits_only) != 9:
        return False

    # Check valid Zimbabwe mobile prefixes
    valid_prefixes = ['71', '73', '77', '78']
    prefix = digits_only[:2]

    # Check if prefix is valid
    if prefix not in valid_prefixes:
        return False

    # Check if all digits are valid (0-9)
    if not digits_only.isdigit():
        return False

    return True


def create_notification(user_id, message):
    notification = Notification(user_id=user_id, message=message)
    db.session.add(notification)
    db.session.commit()


ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "pdf"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def get_available_errands_count(user_id):
    runner_profile = RunnerProfile.query.filter_by(user_id=user_id).first()
    runner_city = runner_profile.city if runner_profile else ""

    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").all()

    return len(available_errands)


def get_consistent_display_count(errands_list):
    if not errands_list:
        return 0, 0

    pending_count = len([e for e in errands_list if e.status == "pending"])
    ongoing_count = len([e for e in errands_list if e.status == "ongoing"])

    if ongoing_count == 0:
        return pending_count, pending_count
    else:
        return pending_count, pending_count


def get_weekly_earnings(runner_id):
    today = datetime.utcnow().date()
    weekly_data = []

    for i in range(7):
        day = today - timedelta(days=i)
        day_earnings = 0.0

        completed_errands = ActiveErrand.query.filter_by(
            runner_id=runner_id,
            status="completed"
        ).all()

        for active_errand in completed_errands:
            if active_errand.end_time and active_errand.end_time.date() == day:
                negotiation = Negotiation.query.filter_by(
                    errand_id=active_errand.errand_id,
                    runner_id=runner_id,
                    status="accepted"
                ).first()

                if negotiation:
                    day_earnings += negotiation.offer_price
                else:
                    day_earnings += active_errand.errand.price_estimate

        weekly_data.append({
            'day': day.strftime('%a'),
            'earnings': day_earnings
        })

    return list(reversed(weekly_data))


def get_monthly_earnings(runner_id):
    monthly_data = []

    for i in range(5, -1, -1):
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        target_month = month_start - timedelta(days=30 * i)

        month_earnings = 0.0

        completed_errands = ActiveErrand.query.filter_by(
            runner_id=runner_id,
            status="completed"
        ).all()

        for active_errand in completed_errands:
            if active_errand.end_time and active_errand.end_time.year == target_month.year and active_errand.end_time.month == target_month.month:
                negotiation = Negotiation.query.filter_by(
                    errand_id=active_errand.errand_id,
                    runner_id=runner_id,
                    status="accepted"
                ).first()

                if negotiation:
                    month_earnings += negotiation.offer_price
                else:
                    month_earnings += active_errand.errand.price_estimate

        monthly_data.append({
            'month': target_month.strftime('%b'),
            'earnings': month_earnings
        })

    return monthly_data


# ============================================================================
# CUSTOM TEMPLATE FILTERS
# ============================================================================

@app.template_filter('timesince')
def timesince_filter(dt):
    if dt is None:
        return 'Recently'

    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except:
            return 'Recently'

    now = datetime.utcnow()
    diff = now - dt

    if diff.days > 365:
        years = diff.days // 365
        return f'{years} year{"s" if years > 1 else ""} ago'
    elif diff.days > 30:
        months = diff.days // 30
        return f'{months} month{"s" if months > 1 else ""} ago'
    elif diff.days > 0:
        return f'{diff.days} day{"s" if diff.days > 1 else ""} ago'
    elif diff.seconds > 3600:
        hours = diff.seconds // 3600
        return f'{hours} hour{"s" if hours > 1 else ""} ago'
    elif diff.seconds > 60:
        minutes = diff.seconds // 60
        return f'{minutes} minute{"s" if minutes > 1 else ""} ago'
    else:
        return 'Just now'


# ============================================================================
# IMPORT MODELS AFTER DATABASE INITIALIZATION
# ============================================================================

from models import User, RunnerProfile, Errand, Negotiation, ActiveErrand, Rating, Notification, AppFeedback


# ============================================================================
# PRICE CALCULATION FUNCTIONS
# ============================================================================

def calculate_grocery_price(budget_limit, item_count):
    base_price = 5.0 + (item_count * 0.5)
    if budget_limit:
        return min(base_price, float(budget_limit) * 0.1)
    return base_price


def calculate_food_delivery_price(item_count, driver_tip):
    base_price = 3.0 + (item_count * 1.0)
    tip = float(driver_tip) if driver_tip else 0.0
    return base_price + tip


def calculate_package_price(weight, timeframe, fragile):
    base_price = 5.0 + (float(weight) * 0.5) if weight else 5.0
    if timeframe == "express":
        base_price *= 1.5
    elif timeframe == "next_day":
        base_price *= 1.2
    if fragile == "yes":
        base_price += 3.0
    elif fragile == "very_fragile":
        base_price += 5.0
    return base_price


def calculate_gadget_service_price(service_type, budget_range):
    base_prices = {
        "diagnostic": 15.0,
        "repair": 25.0,
        "setup": 20.0,
        "data_transfer": 15.0,
        "software_issue": 20.0,
        "purchase_assistance": 10.0,
        "other": 15.0
    }
    return base_prices.get(service_type, 15.0)


def calculate_collections_price(item_count, total_value):
    base_price = 8.0 + (item_count * 0.5)
    if total_value and float(total_value) > 100:
        base_price += 5.0
    return base_price


def calculate_ticket_price(budget_range, ticket_count):
    budget_ranges = {
        "under_20": 15.0, "20_50": 25.0, "50_100": 40.0,
        "100_200": 60.0, "200_plus": 80.0, "flexible": 30.0
    }
    base_price = budget_ranges.get(budget_range, 20.0)
    return base_price + (int(ticket_count) * 2.0) if ticket_count else base_price


def calculate_spare_parts_price(budget_range, part_count):
    budget_ranges = {
        "under_50": 10.0, "50_100": 15.0, "100_200": 20.0,
        "200_500": 25.0, "500_plus": 30.0, "flexible": 15.0
    }
    base_price = budget_ranges.get(budget_range, 15.0)
    return base_price + (part_count * 1.0)


def calculate_gas_price(fuel_type, quantity):
    fuel_prices = {
        "petrol": 1.0, "diesel": 0.8, "premium": 1.2,
        "cng": 0.6, "lpg": 0.7, "other": 1.0
    }
    base_price = 5.0
    fuel_price = fuel_prices.get(fuel_type, 1.0)
    quantity_val = float(quantity) if quantity else 0.0
    return base_price + (fuel_price * quantity_val)


def calculate_other_service_price(budget_range):
    budget_ranges = {
        "under_50": 15.0, "50_100": 25.0, "100_200": 35.0,
        "200_500": 45.0, "500_1000": 60.0, "1000_plus": 80.0,
        "negotiable": 25.0
    }
    return budget_ranges.get(budget_range, 20.0)


# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@app.route("/")
def home():
    if session.get("user_id"):
        user = current_user()
        if user and user.user_type == "runner":
            return redirect(url_for("runnerhome"))
        else:
            return redirect(url_for("home_page"))
    return render_template("index.html")


@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid credentials", "danger")
            return redirect(url_for("signin"))

        session["user_id"] = user.id
        session["user_type"] = user.user_type
        flash(f"Signed in successfully as {user.user_type}", "success")

        if user.user_type == "runner":
            return redirect(url_for("runnerhome"))
        else:
            return redirect(url_for("home_page"))

    return render_template("signin.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        date_of_birth = request.form.get("date_of_birth", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        user_type = request.form.get("user_type", "client")
        terms_agreed = request.form.get("terms_agreed")
        country_code = request.form.get("country_code", "+263")

        # Validate all required fields
        if not all([first_name, last_name, email, phone_number, date_of_birth, username, password, confirm_password,
                    user_type]):
            flash("Please fill all required fields", "danger")
            return redirect(url_for("signup"))

        # MODIFIED: Only check terms agreement for Client users
        if user_type == "client" and not terms_agreed:
            flash("You must agree to the Terms of Service and Privacy Policy", "danger")
            return redirect(url_for("signup"))

        if not validate_email(email):
            flash("Please enter a valid email address", "danger")
            return redirect(url_for("signup"))

        if not validate_phone(phone_number):
            flash(
                "Please enter a valid Zimbabwe mobile number. Must be 9 digits starting with 71, 73, 77, or 78 (e.g., 771234567)",
                "danger")
            return redirect(url_for("signup"))

        if len(password) < 6:
            flash("Password must be at least 6 characters", "danger")
            return redirect(url_for("signup"))

        if password != confirm_password:
            flash("Passwords do not match", "danger")
            return redirect(url_for("signup"))

        # Validate date of birth
        try:
            dob = datetime.strptime(date_of_birth, "%Y-%m-%d")
            age = datetime.now().year - dob.year - ((datetime.now().month, datetime.now().day) < (dob.month, dob.day))
            if age < 13:
                flash("You must be at least 13 years old to register", "danger")
                return redirect(url_for("signup"))
        except ValueError:
            flash("Please enter a valid date of birth", "danger")
            return redirect(url_for("signup"))

        # Check if username or email already exists
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already exists", "danger")
            return redirect(url_for("signup"))

        # Create new user
        user = User(
            fullname=f"{first_name} {last_name}",
            email=email,
            phone=f"{country_code}{phone_number}",
            username=username,
            password_hash=generate_password_hash(password),
            user_type=user_type
        )
        db.session.add(user)
        db.session.commit()

        if user_type == "runner":
            session["user_id"] = user.id
            session["user_type"] = user.user_type
            session["temp_signup_data"] = {
                "user_id": user.id,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": f"{country_code}{phone_number}",
                "date_of_birth": date_of_birth,
                "username": username,
                "user_type": user_type
            }
            flash("Registration successful! Please sign in to continue.", "success")
            return redirect(url_for("signin"))
        else:
            session["user_id"] = user.id
            session["user_type"] = user.user_type
            flash("Registration successful! Welcome to ErrandGo.", "success")
            return redirect(url_for("home_page"))

    return render_template("signup.html")


@app.route("/runner_signup", methods=["GET", "POST"])
@login_required
def runner_signup():
    user = current_user()

    if not user or user.user_type != "runner":
        flash("Please sign up as a runner first", "warning")
        return redirect(url_for("signup"))

    existing_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    if existing_profile:
        flash("You already have a runner profile.", "info")
        return redirect(url_for("runnerhome"))

    if request.method == "POST":
        dob = request.form.get("dob")
        address = request.form.get("address")
        id_number = request.form.get("id_number")
        vehicle_type = request.form.get("vehicle_type")
        city = request.form.get("city")
        preferred_routes = request.form.get("preferred_routes", "")
        license_photo = request.files.get("license_photo")
        id_photo = request.files.get("id_photo")

        if not all([dob, address, id_number, vehicle_type, city]):
            flash("Please fill all required fields", "danger")
            return redirect(url_for("runner_signup"))

        if not license_photo or not allowed_file(license_photo.filename):
            flash("Please upload a valid license photo", "danger")
            return redirect(url_for("runner_signup"))

        if not id_photo or not allowed_file(id_photo.filename):
            flash("Please upload a valid ID photo", "danger")
            return redirect(url_for("runner_signup"))

        filename_license = None
        filename_id = None
        if license_photo and allowed_file(license_photo.filename):
            filename_license = f"{user.id}_license_{secure_filename(license_photo.filename)}"
            license_photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename_license))
        if id_photo and allowed_file(id_photo.filename):
            filename_id = f"{user.id}_id_{secure_filename(id_photo.filename)}"
            id_photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename_id))

        try:
            if dob:
                dob_date = datetime.strptime(dob, "%Y-%m-%d")
                age = datetime.now().year - dob_date.year - (
                        (datetime.now().month, datetime.now().day) < (dob_date.month, dob_date.day))
                if age < 18:
                    flash("You must be at least 18 years old to be a runner", "danger")
                    return redirect(url_for("runner_signup"))
        except ValueError:
            flash("Please enter a valid date of birth", "danger")
            return redirect(url_for("runner_signup"))

        profile = RunnerProfile(
            user_id=user.id,
            dob=dob,
            address=address,
            id_number=id_number,
            vehicle_type=vehicle_type,
            city=city,
            preferred_routes=preferred_routes,
            license_photo=filename_license,
            id_photo=filename_id
        )
        db.session.add(profile)
        db.session.commit()

        if "temp_signup_data" in session:
            session.pop("temp_signup_data", None)

        session["user_id"] = user.id
        session["user_type"] = user.user_type

        flash("Runner profile completed successfully! Welcome to ErrandGo.", "success")
        return redirect(url_for("runnerhome"))

    return render_template("runner_signup.html", user=user)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("signin"))


# ============================================================================
# MAIN CLIENT ROUTES
# ============================================================================

@app.route("/home")
@login_required
def home_page():
    user = current_user()

    if not user:
        return redirect(url_for("signin"))

    if user.user_type == "runner":
        runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
        if not runner_profile:
            flash("Please complete your runner profile first", "warning")
            return redirect(url_for("runner_signup"))
        return redirect(url_for("runnerhome"))

    notifications = Notification.query.filter_by(user_id=user.id, is_read=False).order_by(
        Notification.created_at.desc()).limit(5).all()

    all_user_errands = Errand.query.filter_by(client_id=user.id).all()

    pending_count = len([e for e in all_user_errands if e.status == "pending"])
    completed_count = len([e for e in all_user_errands if e.status == "completed"])

    recent_errands = Errand.query.filter_by(client_id=user.id).order_by(Errand.created_at.desc()).limit(5).all()

    return render_template(
        "home.html",
        user=user,
        recent_errands=recent_errands,
        notifications=notifications,
        pending_count=pending_count,
        completed_count=completed_count
    )


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type == "client":
        return render_template("dashboard_client.html", user=user)
    else:
        runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
        if not runner_profile:
            return redirect(url_for("runner_signup"))
        return redirect(url_for("dashboardrunner"))


@app.route("/order_history")
@login_required
def order_history():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type == "client":
        all_errands = Errand.query.filter_by(client_id=user.id).all()
        errands = Errand.query.filter_by(client_id=user.id).order_by(Errand.created_at.desc()).all()
    else:
        active_errands = ActiveErrand.query.filter_by(runner_id=user.id).all()
        errand_ids = [ae.errand_id for ae in active_errands]
        all_errands = Errand.query.filter(Errand.id.in_(errand_ids)).all()
        errands = Errand.query.filter(Errand.id.in_(errand_ids)).order_by(Errand.created_at.desc()).all()

    pending_count = len([e for e in all_errands if e.status == "pending"])
    ongoing_count = len([e for e in all_errands if e.status == "ongoing" or e.status == "accepted"])
    completed_count = len([e for e in all_errands if e.status == "completed"])

    orders_with_details = []
    for errand in errands:
        if user.user_type == "client":
            active_errand = ActiveErrand.query.filter_by(errand_id=errand.id).first()
            runner = User.query.get(active_errand.runner_id) if active_errand else None
            other_user = runner
        else:
            client = User.query.get(errand.client_id)
            other_user = client

        rating = Rating.query.filter_by(errand_id=errand.id).first()

        service_icons = {
            "Grocery Shopping": "shopping-basket",
            "Food Delivery": "utensils",
            "Package Delivery": "box",
            "Bill Payments": "file-invoice-dollar",
            "Gadget Services": "laptop",
            "Collections": "box-open",
            "Ticket Booking": "ticket-alt",
            "Spare Parts": "cog",
            "Gas Delivery": "gas-pump",
            "General Purchasing": "shopping-bag",
            "Property Purchase - Furniture": "home",
            "Property Purchase - Appliance": "blender",
            "Property Purchase - Electronics": "tv",
            "Property Purchase - Tools": "tools",
            "Property Purchase - Decor": "paint-roller",
            "Property Purchase - Other": "box"
        }

        orders_with_details.append({
            'order_id': errand.id,
            'service_type': errand.type,
            'service_icon': service_icons.get(errand.type, "running"),
            'date': errand.created_at if errand.status == "completed" else errand.created_at,
            'status': errand.status,
            'amount': errand.price_estimate,
            'rating': rating.rating if rating else None,
            'other_user': other_user,
            'pickup_location': errand.pickup_location,
            'delivery_location': errand.delivery_location
        })

    total_orders = len(orders_with_details)
    total_spent = sum(order['amount'] for order in orders_with_details if order['amount'])

    ratings = [order['rating'] for order in orders_with_details if order['rating']]
    avg_rating = sum(ratings) / len(ratings) if ratings else 0

    return render_template(
        "order_history.html",
        user=user,
        orders=orders_with_details,
        total_orders=total_orders,
        total_spent=total_spent,
        avg_rating=round(avg_rating, 1),
        pending_count=pending_count,
        ongoing_count=ongoing_count,
        completed_count=completed_count
    )


@app.route('/errands/completed')
@login_required
def completed_errands():
    user = current_user()

    if user.user_type == 'client':
        completed_errands_list = Errand.query.filter(
            Errand.client_id == user.id,
            Errand.status == 'completed'
        ).order_by(Errand.created_at.desc()).all()
    else:
        completed_active = ActiveErrand.query.filter_by(
            runner_id=user.id,
            status='completed'
        ).order_by(ActiveErrand.end_time.desc()).all()
        completed_errands_list = [Errand.query.get(ae.errand_id) for ae in completed_active]
        completed_errands_list = [e for e in completed_errands_list if e]

    total_earned = sum(e.price_estimate or 0 for e in completed_errands_list)

    ratings = Rating.query.filter_by(to_user_id=user.id).all()
    average_rating = sum(r.rating for r in ratings) / len(ratings) if ratings else 0

    return render_template('completed.html',
                           completed_errands=completed_errands_list,
                           user_type=user.user_type,
                           total_earned=total_earned,
                           average_rating=average_rating)


@app.route("/completed_errands")
def old_completed_errands():
    return redirect(url_for("completed_errands"))


@app.route("/completed")
def completed():
    return redirect(url_for("completed_errands"))


# ============================================================================
# RUNNER ROUTES
# ============================================================================

@app.route("/runnerhome")
@login_required
def runnerhome():
    user = current_user()

    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    if not runner_profile:
        flash("Please complete your runner profile first", "warning")
        return redirect(url_for("runner_signup"))

    active_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="ongoing").all()

    runner_city = runner_profile.city if runner_profile else ""

    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).order_by(Errand.created_at.desc()).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").order_by(Errand.created_at.desc()).all()

    available_count = get_available_errands_count(user.id)

    completed_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").all()
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

    today = datetime.utcnow().date()
    today_earnings = 0.0
    for active_errand in completed_errands:
        if active_errand.end_time and active_errand.end_time.date() == today:
            negotiation = Negotiation.query.filter_by(
                errand_id=active_errand.errand_id,
                runner_id=user.id,
                status="accepted"
            ).first()
            if negotiation:
                today_earnings += negotiation.offer_price
            else:
                today_earnings += active_errand.errand.price_estimate

    notifications = Notification.query.filter_by(user_id=user.id, is_read=False).order_by(
        Notification.created_at.desc()).limit(5).all()

    return render_template(
        "runnerhome.html",
        user=user,
        active_errands=active_errands,
        available_errands=available_errands,
        completed_count=completed_count,
        active_count=active_count,
        total_earnings=total_earnings,
        today_earnings=today_earnings,
        notifications=notifications,
        runner_profile=runner_profile,
        available_count=available_count
    )


@app.route("/dashboardrunner")
@login_required
def dashboardrunner():
    user = current_user()

    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    if not runner_profile:
        flash("Please complete your runner profile first", "warning")
        return redirect(url_for("runner_signup"))

    active_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="ongoing").all()

    runner_city = runner_profile.city if runner_profile else ""

    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).order_by(Errand.created_at.desc()).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").order_by(Errand.created_at.desc()).all()

    available_count = get_available_errands_count(user.id)

    completed_errands = ActiveErrand.query.filter_by(runner_id=user.id, status="completed").all()
    completed_count = len(completed_errands)
    active_count = len(active_errands)

    total_earnings = 0.0
    today_earnings = 0.0
    today = datetime.utcnow().date()

    for active_errand in completed_errands:
        negotiation = Negotiation.query.filter_by(
            errand_id=active_errand.errand_id,
            runner_id=user.id,
            status="accepted"
        ).first()

        if negotiation:
            earnings = negotiation.offer_price
        else:
            earnings = active_errand.errand.price_estimate

        total_earnings += earnings

        if active_errand.end_time and active_errand.end_time.date() == today:
            today_earnings += earnings

    weekly_earnings = get_weekly_earnings(user.id)

    notifications = Notification.query.filter_by(user_id=user.id, is_read=False).order_by(
        Notification.created_at.desc()).limit(5).all()

    ratings = Rating.query.filter_by(to_user_id=user.id).all()
    avg_rating = sum(r.rating for r in ratings) / len(ratings) if ratings else 4.8

    return render_template(
        "runnerdashboard.html",
        user=user,
        active_errands=active_errands,
        available_errands=available_errands,
        completed_count=completed_count,
        active_count=active_count,
        available_count=available_count,
        total_earnings=total_earnings,
        today_earnings=today_earnings,
        weekly_earnings=weekly_earnings,
        notifications=notifications,
        runner_profile=runner_profile,
        avg_rating=round(avg_rating, 1),
        rating_count=len(ratings)
    )


@app.route("/runnercompleted")
@login_required
def runnercompleted():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    completed_active_errands = ActiveErrand.query.filter_by(
        runner_id=user.id,
        status="completed"
    ).order_by(ActiveErrand.end_time.desc()).all()

    errands_with_details = []
    for active_errand in completed_active_errands:
        errand = Errand.query.get(active_errand.errand_id)
        client = User.query.get(errand.client_id) if errand else None

        existing_rating = Rating.query.filter_by(
            errand_id=errand.id,
            from_user_id=user.id
        ).first()

        client_rating = Rating.query.filter_by(
            errand_id=errand.id,
            from_user_id=errand.client_id,
            to_user_id=user.id
        ).first()

        errands_with_details.append({
            'errand': errand,
            'client': client,
            'active_errand': active_errand,
            'rating_status': 'rated' if existing_rating else 'pending',
            'rating': existing_rating.rating if existing_rating else None,
            'client_rating': client_rating.rating if client_rating else None,
            'client_comment': client_rating.comment if client_rating else None
        })

    completed_count = len(errands_with_details)
    total_earnings = 0.0
    pending_ratings = 0

    for errand_data in errands_with_details:
        negotiation = Negotiation.query.filter_by(
            errand_id=errand_data['errand'].id,
            runner_id=user.id,
            status="accepted"
        ).first()

        if negotiation:
            total_earnings += negotiation.offer_price
        else:
            total_earnings += errand_data['errand'].price_estimate

        if errand_data['rating_status'] == 'pending':
            pending_ratings += 1

    return render_template(
        "runnercompleted.html",
        user=user,
        completions=errands_with_details,
        completed_count=completed_count,
        total_earnings=total_earnings,
        pending_ratings=pending_ratings
    )


@app.route("/runneravailable_errands")
@login_required
def runneravailable_errands():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    runner_city = runner_profile.city if runner_profile else ""

    if runner_city:
        available_errands = Errand.query.filter(
            Errand.status == "pending",
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).order_by(Errand.created_at.desc()).all()
    else:
        available_errands = Errand.query.filter_by(status="pending").order_by(Errand.created_at.desc()).all()

    available_count = get_available_errands_count(user.id)

    errands_data = []
    for errand in available_errands:
        client = User.query.get(errand.client_id)

        existing_negotiation = Negotiation.query.filter_by(
            errand_id=errand.id,
            runner_id=user.id
        ).first()

        errand_data = {
            'errand': {
                'id': errand.id,
                'type': errand.type,
                'pickup_location': errand.pickup_location,
                'delivery_location': errand.delivery_location,
                'weight': errand.weight,
                'delivery_time': errand.delivery_time,
                'details': errand.details,
                'price_estimate': float(errand.price_estimate) if errand.price_estimate else 0.0,
                'status': errand.status,
                'created_at': errand.created_at.isoformat() if errand.created_at else None
            },
            'client': {
                'id': client.id,
                'fullname': client.fullname,
                'username': client.username,
                'email': client.email
            } if client else None,
            'has_offered': existing_negotiation is not None,
            'offer_status': existing_negotiation.status if existing_negotiation else None
        }
        errands_data.append(errand_data)

    return render_template(
        "runneravailable_errands.html",
        user=user,
        available_errands=errands_data,
        runner_city=runner_city,
        available_count=available_count
    )


@app.route("/runnerhistory")
@login_required
def runnerhistory():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    active_errands = ActiveErrand.query.filter_by(runner_id=user.id).order_by(ActiveErrand.start_time.desc()).all()

    orders_with_details = []
    for active_errand in active_errands:
        errand = Errand.query.get(active_errand.errand_id)
        client = User.query.get(errand.client_id) if errand else None

        negotiation = Negotiation.query.filter_by(
            errand_id=errand.id,
            runner_id=user.id,
            status="accepted"
        ).first()

        rating = Rating.query.filter_by(
            errand_id=errand.id,
            from_user_id=client.id,
            to_user_id=user.id
        ).first()

        if negotiation:
            final_price = negotiation.offer_price
        else:
            final_price = errand.price_estimate

        service_icons = {
            "Grocery Shopping": "shopping-basket",
            "Food Delivery": "utensils",
            "Package Delivery": "box",
            "Bill Payments": "file-invoice-dollar",
            "Gadget Services": "laptop",
            "Collections": "box-open",
            "Ticket Booking": "ticket-alt",
            "Spare Parts": "cog",
            "Gas Delivery": "gas-pump",
            "General Purchasing": "shopping-bag",
            "Property Purchase - Furniture": "home",
            "Property Purchase - Appliance": "blender",
            "Property Purchase - Electronics": "tv",
            "Property Purchase - Tools": "tools",
            "Property Purchase - Decor": "paint-roller",
            "Property Purchase - Other": "box"
        }

        orders_with_details.append({
            'order_id': errand.id,
            'service_type': errand.type,
            'service_icon': service_icons.get(errand.type, "running"),
            'start_date': active_errand.start_time,
            'end_date': active_errand.end_time,
            'status': active_errand.status,
            'amount': final_price,
            'rating': rating.rating if rating else None,
            'client': client,
            'pickup_location': errand.pickup_location,
            'delivery_location': errand.delivery_location,
            'active_errand': active_errand
        })

    total_orders = len(orders_with_details)
    completed_orders = len([o for o in orders_with_details if o['status'] == 'completed'])
    ongoing_orders = len([o for o in orders_with_details if o['status'] == 'ongoing'])

    total_earnings = sum(order['amount'] for order in orders_with_details if order['status'] == 'completed')

    ratings = [order['rating'] for order in orders_with_details if order['rating']]
    avg_rating = sum(ratings) / len(ratings) if ratings else 0

    monthly_earnings = get_monthly_earnings(user.id)

    return render_template(
        "runnerhistory.html",
        user=user,
        orders=orders_with_details,
        total_orders=total_orders,
        completed_orders=completed_orders,
        ongoing_orders=ongoing_orders,
        total_earnings=total_earnings,
        avg_rating=round(avg_rating, 1),
        monthly_earnings=monthly_earnings
    )


@app.route("/runnerwallet")
@login_required
def runnerwallet():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    completed_errands = ActiveErrand.query.filter_by(
        runner_id=user.id,
        status="completed"
    ).all()

    balance = 0.0
    pending_payouts = 0.0
    transactions = []

    for active_errand in completed_errands:
        errand = Errand.query.get(active_errand.errand_id)
        negotiation = Negotiation.query.filter_by(
            errand_id=errand.id,
            runner_id=user.id,
            status="accepted"
        ).first()

        amount = negotiation.offer_price if negotiation else errand.price_estimate

        is_pending = active_errand.end_time and (datetime.utcnow() - active_errand.end_time).days < 7

        if is_pending:
            pending_payouts += amount
        else:
            balance += amount

        transactions.append({
            'date': active_errand.end_time or active_errand.start_time,
            'description': f"{errand.type} - {errand.pickup_location} to {errand.delivery_location}",
            'amount': amount,
            'status': 'pending' if is_pending else 'completed',
            'type': 'credit'
        })

    transactions.sort(key=lambda x: x['date'], reverse=True)

    return render_template(
        "runnerwallet.html",
        user=user,
        balance=balance,
        pending_payouts=pending_payouts,
        transactions=transactions,
        total_earnings=balance + pending_payouts
    )


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


# ============================================================================
# SERVICE CREATION ROUTES
# ============================================================================

@app.route("/services")
@login_required
def services():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("home_page"))

    services_list = [
        {"name": "Grocery Shopping", "endpoint": "create_grocery_errand", "icon": "shopping-basket",
         "description": "Get your groceries delivered"},
        {"name": "Food Delivery", "endpoint": "create_food_delivery_errand", "icon": "utensils",
         "description": "Restaurant food delivery"},
        {"name": "Package Delivery", "endpoint": "create_package_delivery_errand", "icon": "box",
         "description": "Send packages and parcels"},
        {"name": "Bill Payments", "endpoint": "create_bill_payment_errand", "icon": "file-invoice-dollar",
         "description": "Pay bills and utilities"},
        {"name": "Gadget Services", "endpoint": "create_gadget_service_errand", "icon": "laptop",
         "description": "Repair and tech services"},
        {"name": "Collections", "endpoint": "create_collections_errand", "icon": "box-open",
         "description": "Pick up and collect items"},
        {"name": "Ticket Booking", "endpoint": "create_ticket_booking_errand", "icon": "ticket-alt",
         "description": "Book event or travel tickets"},
        {"name": "Spare Parts", "endpoint": "create_spare_parts_errand", "icon": "cog",
         "description": "Find and deliver spare parts"},
        {"name": "Gas Delivery", "endpoint": "create_gas_delivery_errand", "icon": "gas-pump",
         "description": "Fuel and gas delivery"},
        {"name": "General Purchasing", "endpoint": "create_purchase_errand", "icon": "shopping-bag",
         "description": "We buy and deliver items"},
        {"name": "Property Purchase", "endpoint": "create_property_errand", "icon": "home",
         "description": "Furniture & appliances delivery"},
        {"name": "Other Services", "endpoint": "create_other_service_errand", "icon": "running",
         "description": "Custom errands and services"}
    ]

    return render_template("dashboard.html", user=user, services=services_list)


@app.route("/create_errand")
def create_errand():
    return redirect(url_for("services"))


@app.route("/create_grocery_errand", methods=["GET", "POST"])
@login_required
def create_grocery_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        store_preference = request.form.get("store_preference")
        store_location = request.form.get("store_location")
        items = request.form.getlist("items[]")
        budget_limit = request.form.get("budget_limit")
        delivery_address = request.form.get("delivery_address")
        special_instructions = request.form.get("special_instructions")

        errand = Errand(
            client_id=user.id,
            type="Grocery Shopping",
            pickup_location=store_location,
            delivery_location=delivery_address,
            details=f"Grocery shopping at {store_preference}. Items: {', '.join(items)}. Budget: ${budget_limit}. Instructions: {special_instructions}",
            price_estimate=calculate_grocery_price(budget_limit, len(items)),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Grocery errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("grocery.html", user=user)


@app.route("/create_food_delivery_errand", methods=["GET", "POST"])
@login_required
def create_food_delivery_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        restaurant_name = request.form.get("restaurant_name")
        restaurant_address = request.form.get("restaurant_address")
        items = request.form.getlist("items[]")
        delivery_address = request.form.get("delivery_address")
        driver_tip = request.form.get("driver_tip")
        special_requests = request.form.get("special_requests")

        errand = Errand(
            client_id=user.id,
            type="Food Delivery",
            pickup_location=restaurant_address,
            delivery_location=delivery_address,
            details=f"Food from {restaurant_name}. Items: {', '.join(items)}. Instructions: {special_requests}",
            price_estimate=calculate_food_delivery_price(len(items), driver_tip),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Food delivery errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("food_delivery.html", user=user)


@app.route("/create_bill_payment_errand", methods=["GET", "POST"])
@login_required
def create_bill_payment_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        bill_types = request.form.getlist("bill_types[]")
        service_provider = request.form.get("service_provider")
        total_amount = request.form.get("total_amount")
        payment_location = request.form.get("payment_location")

        errand = Errand(
            client_id=user.id,
            type="Bill Payments",
            pickup_location=payment_location,
            delivery_location="N/A",
            details=f"Pay bills for {service_provider}. Total: ${total_amount}. Bills: {', '.join(bill_types)}",
            price_estimate=float(total_amount) if total_amount else 0.0,
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Bill payment errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("bill_payments.html", user=user)


@app.route("/create_package_delivery_errand", methods=["GET", "POST"])
@login_required
def create_package_delivery_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        package_description = request.form.get("package_description")
        weight = request.form.get("weight")
        pickup_address = request.form.get("pickup_address")
        delivery_address = request.form.get("delivery_address")
        delivery_timeframe = request.form.get("delivery_timeframe")
        fragile_items = request.form.get("fragile_items")

        errand = Errand(
            client_id=user.id,
            type="Package Delivery",
            pickup_location=pickup_address,
            delivery_location=delivery_address,
            details=f"Package: {package_description}. Special handling: {fragile_items}",
            price_estimate=calculate_package_price(weight, delivery_timeframe, fragile_items),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Package delivery errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("package_delivery.html", user=user)


@app.route("/create_gadget_service_errand", methods=["GET", "POST"])
@login_required
def create_gadget_service_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        device_type = request.form.get("device_type")
        brand = request.form.get("brand")
        model = request.form.get("model")
        part_names = request.form.getlist("part_names[]")
        budget_range = request.form.get("budget_range")
        delivery_address = request.form.get("delivery_address")

        errand = Errand(
            client_id=user.id,
            type="Gadget Services",
            pickup_location="Various suppliers",
            delivery_location=delivery_address,
            details=f"Find {len(part_names)} parts for {brand} {model}. Parts: {', '.join(part_names)}",
            price_estimate=calculate_gadget_service_price("purchase_assistance", budget_range),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Gadget service errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("gadget_service.html", user=user)


@app.route("/create_collections_errand", methods=["GET", "POST"])
@login_required
def create_collections_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        item_descriptions = request.form.getlist("item_descriptions[]")
        collection_location = request.form.get("collection_location")
        recipient_address = request.form.get("recipient_address")
        total_value = request.form.get("total_value")

        errand = Errand(
            client_id=user.id,
            type="Collections",
            pickup_location=collection_location,
            delivery_location=recipient_address,
            details=f"Collect {len(item_descriptions)} items from {collection_location}",
            price_estimate=calculate_collections_price(len(item_descriptions), total_value),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Collection errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("Collections.html", user=user)


@app.route("/create_ticket_booking_errand", methods=["GET", "POST"])
@login_required
def create_ticket_booking_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        event_name = request.form.get("event_name")
        venue = request.form.get("venue")
        number_of_tickets = request.form.get("number_of_tickets")
        budget_range = request.form.get("budget_range")
        delivery_address = request.form.get("delivery_address")

        errand = Errand(
            client_id=user.id,
            type="Ticket Booking",
            pickup_location="N/A",
            delivery_location=delivery_address,
            details=f"Book {number_of_tickets} tickets for {event_name} at {venue}",
            price_estimate=calculate_ticket_price(budget_range, number_of_tickets),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Ticket booking errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("ticket_booking.html", user=user)


@app.route("/create_spare_parts_errand", methods=["GET", "POST"])
@login_required
def create_spare_parts_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        make = request.form.get("make")
        model = request.form.get("model")
        part_names = request.form.getlist("part_names[]")
        budget_range = request.form.get("budget_range")
        delivery_address = request.form.get("delivery_address")

        errand = Errand(
            client_id=user.id,
            type="Spare Parts",
            pickup_location="Various suppliers",
            delivery_location=delivery_address,
            details=f"Find {len(part_names)} parts for {make} {model}. Parts: {', '.join(part_names)}",
            price_estimate=calculate_spare_parts_price(budget_range, len(part_names)),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Spare parts errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("spare_parts.html", user=user)


@app.route("/create_gas_delivery_errand", methods=["GET", "POST"])
@login_required
def create_gas_delivery_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        fuel_type = request.form.get("fuel_type")
        quantity = request.form.get("quantity")
        delivery_address = request.form.get("delivery_address")

        errand = Errand(
            client_id=user.id,
            type="Gas Delivery",
            pickup_location="Fuel station",
            delivery_location=delivery_address,
            details=f"Deliver {quantity} of {fuel_type} to {delivery_address}",
            price_estimate=calculate_gas_price(fuel_type, quantity),
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Gas delivery errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("gas_delivery.html", user=user)


@app.route("/create_purchase_errand", methods=["GET", "POST"])
@login_required
def purchase_page():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("home_page"))

    if request.method == "POST":
        store_name = request.form.get("store_name")
        store_location = request.form.get("store_location")
        items = request.form.getlist("items[]")
        quantities = request.form.getlist("quantities[]")
        brands = request.form.getlist("brands[]")
        prices = request.form.getlist("prices[]")
        budget_limit = request.form.get("budget_limit")
        estimated_weight = request.form.get("estimated_weight")
        delivery_address = request.form.get("delivery_address")
        delivery_time = request.form.get("delivery_time")
        special_instructions = request.form.get("special_instructions")
        contact_number = request.form.get("contact_number")

        item_details = []
        for i in range(len(items)):
            if items[i].strip():
                item_str = f"{items[i]}"
                if quantities[i].strip():
                    item_str += f" (Qty: {quantities[i]})"
                if brands[i].strip():
                    item_str += f" - Brand: {brands[i]}"
                if prices[i].strip():
                    item_str += f" - Est. Price: ${prices[i]}"
                item_details.append(item_str)

        details = f"Purchase items from {store_name} at {store_location}.\n"
        details += f"Items to purchase: {'; '.join(item_details)}\n"
        details += f"Budget limit: ${budget_limit if budget_limit else 'Not specified'}\n"
        details += f"Estimated weight: {estimated_weight} lbs\n"
        details += f"Delivery time: {delivery_time}\n"
        details += f"Special instructions: {special_instructions}"

        total_item_value = 0
        for i in range(len(prices)):
            if prices[i].strip():
                price = float(prices[i]) if prices[i] else 0
                quantity = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
                total_item_value += price * quantity

        service_fee = min(max(total_item_value * 0.15, 10), 50)
        weight_fee = 0
        if estimated_weight:
            weight_val = float(estimated_weight)
            if weight_val <= 50:
                weight_fee = 3.00
            elif weight_val <= 100:
                weight_fee = 5.00
            elif weight_val <= 200:
                weight_fee = 8.00
            elif weight_val <= 500:
                weight_fee = 15.00
            else:
                weight_fee = 25.00

        time_fee = 0
        if delivery_time == "asap":
            time_fee = 3.00
        elif delivery_time == "morning":
            time_fee = 2.50
        elif delivery_time == "afternoon":
            time_fee = 2.00
        elif delivery_time == "evening":
            time_fee = 2.75

        total_price = service_fee + weight_fee + time_fee + 0.50

        errand = Errand(
            client_id=user.id,
            type="General Purchasing",
            pickup_location=store_location,
            delivery_location=delivery_address,
            details=details,
            price_estimate=total_price,
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("General purchasing errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("purchase.html", user=user)


@app.route("/create_property_errand", methods=["GET", "POST"])
@login_required
def property_page():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("home_page"))

    if request.method == "POST":
        service_type = request.form.get("service_type")
        store_name = request.form.get("store_name")
        store_location = request.form.get("store_location")
        collection_location = request.form.get("collection_location")
        items = request.form.getlist("items[]")
        quantities = request.form.getlist("quantities[]")
        brands = request.form.getlist("brands[]")
        prices = request.form.getlist("prices[]")
        budget_limit = request.form.get("budget_limit")
        estimated_weight = request.form.get("estimated_weight")
        dimensions = request.form.get("dimensions")
        property_type = request.form.get("property_type")
        assembly_required = request.form.get("assembly_required") == "on"
        delivery_address = request.form.get("delivery_address")
        delivery_time = request.form.get("delivery_time")
        delivery_location_details = request.form.get("delivery_location")
        special_instructions = request.form.get("special_instructions")
        contact_number = request.form.get("contact_number")

        item_details = []
        for i in range(len(items)):
            if items[i].strip():
                item_str = f"{items[i]}"
                if quantities[i].strip():
                    item_str += f" (Qty: {quantities[i]})"
                if brands[i].strip():
                    item_str += f" - Brand: {brands[i]}"
                if prices[i].strip():
                    item_str += f" - Est. Value: ${prices[i]}"
                item_details.append(item_str)

        if service_type == "buy-deliver":
            details = f"Buy & Deliver property items from {store_name} at {store_location}.\n"
        else:
            details = f"Collect & Deliver property items from {collection_location}.\n"

        details += f"Property type: {property_type}\n"
        details += f"Items: {'; '.join(item_details)}\n"
        details += f"Budget limit: ${budget_limit if budget_limit else 'Not specified'}\n"
        details += f"Estimated weight: {estimated_weight} lbs\n"
        details += f"Dimensions: {dimensions}\n"
        details += f"Assembly required: {'Yes' if assembly_required else 'No'}\n"
        details += f"Delivery location: {delivery_location_details}\n"
        details += f"Delivery time: {delivery_time}\n"
        details += f"Special instructions: {special_instructions}"

        total_item_value = 0
        for i in range(len(prices)):
            if prices[i].strip():
                price = float(prices[i]) if prices[i] else 0
                quantity = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
                total_item_value += price * quantity

        service_fee = min(max(total_item_value * 0.15, 10), 50)

        weight_fee = 0
        if estimated_weight:
            weight_val = float(estimated_weight)
            if weight_val <= 50:
                weight_fee = 3.00
            elif weight_val <= 100:
                weight_fee = 5.00
            elif weight_val <= 200:
                weight_fee = 8.00
            elif weight_val <= 500:
                weight_fee = 15.00
            else:
                weight_fee = 25.00

        complexity_fee = 0
        if property_type == "furniture":
            complexity_fee = 10.00
        elif property_type == "appliance":
            complexity_fee = 8.00
        elif property_type == "electronics":
            complexity_fee = 5.00
        elif property_type == "tools":
            complexity_fee = 3.00
        elif property_type == "decor":
            complexity_fee = 2.00
        else:
            complexity_fee = 4.00

        assembly_fee = 0
        if assembly_required:
            if property_type == "furniture":
                assembly_fee = 15.00
            elif property_type == "appliance":
                assembly_fee = 20.00
            elif property_type == "electronics":
                assembly_fee = 10.00
            else:
                assembly_fee = 8.00

        time_fee = 0
        if delivery_time == "asap":
            time_fee = 5.00
        elif delivery_time == "morning":
            time_fee = 4.00
        elif delivery_time == "afternoon":
            time_fee = 3.50
        elif delivery_time == "evening":
            time_fee = 4.50

        total_price = service_fee + weight_fee + complexity_fee + assembly_fee + time_fee

        errand = Errand(
            client_id=user.id,
            type=f"Property Purchase - {property_type.capitalize()}",
            pickup_location=store_location if service_type == "buy-deliver" else collection_location,
            delivery_location=delivery_address,
            details=details,
            price_estimate=total_price,
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        flash("Property purchase errand created successfully!", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("property.html", user=user)


@app.route("/create_other_service_errand", methods=["GET", "POST"])
@login_required
def create_other_service_errand():
    user = current_user()
    if not user or user.user_type != "client":
        flash("Only clients can create errands", "warning")
        return redirect(url_for("signin"))

    if request.method == "POST":
        errand_type = request.form.get("errand_type")

        if errand_type == 'other':
            custom_type = request.form.get("other_errand_type", "").strip()
            errand_type = custom_type if custom_type else "Custom Errand"

        pickup = request.form.get("pickup_location")
        delivery = request.form.get("delivery_location")
        weight = request.form.get("luggage")
        delivery_time = request.form.get("delivery_time")
        details = request.form.get("errand_details")

        base_prices = {"small": 5.0, "medium": 10.0, "large": 15.0, "xlarge": 25.0}
        time_multipliers = {"instant": 1.5, "same-day": 1.2, "next-day": 1.0}
        price = base_prices.get(weight, 5.0) * time_multipliers.get(delivery_time, 1.0)

        errand = Errand(
            client_id=user.id,
            type=errand_type,
            pickup_location=pickup,
            delivery_location=delivery,
            weight=weight,
            delivery_time=delivery_time,
            details=details,
            price_estimate=price,
            status="pending"
        )
        db.session.add(errand)
        db.session.commit()

        create_notification(user.id, f"Errand created: {errand_type}. Waiting for runners to accept.")
        flash("Errand created. Find a runner to accept it.", "success")
        return redirect(url_for("available_runners", errand_id=errand.id))

    return render_template("other.html", service_type="other", service_display="Other Services", user=user)


@app.route("/create_custom_errand", methods=["GET", "POST"])
def create_custom_errand():
    if request.method == "POST":
        return redirect(url_for("create_other_service_errand"))
    return redirect(url_for("create_other_service_errand"))


@app.route("/other_services")
def other_services():
    return redirect(url_for("create_other_service_errand"))


# ============================================================================
# ERRAND MANAGEMENT & NEGOTIATION ROUTES
# ============================================================================

@app.route("/available_runners")
@login_required
def available_runners():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "client":
        flash("Only clients can view this page", "warning")
        return redirect(url_for("signin"))

    errand_id = request.args.get("errand_id", type=int)
    errand = Errand.query.get(errand_id) if errand_id else None

    if not errand:
        flash("Errand not found", "danger")
        return redirect(url_for("home_page"))

    pickup_city = errand.pickup_location.split(",")[-1].strip() if errand.pickup_location else ""

    if pickup_city:
        runners_with_data = db.session.query(
            User,
            RunnerProfile,
            db.func.count(ActiveErrand.id).label('total_errands'),
            db.func.sum(db.case((ActiveErrand.status == 'completed', 1), else_=0)).label('completed_errands'),
            db.func.avg(Rating.rating).label('avg_rating')
        ).join(
            RunnerProfile, User.id == RunnerProfile.user_id
        ).outerjoin(
            ActiveErrand, User.id == ActiveErrand.runner_id
        ).outerjoin(
            Rating, User.id == Rating.to_user_id
        ).filter(
            RunnerProfile.city.ilike(f"%{pickup_city}%")
        ).group_by(
            User.id, RunnerProfile.id
        ).all()
    else:
        runners_with_data = db.session.query(
            User,
            RunnerProfile,
            db.func.count(ActiveErrand.id).label('total_errands'),
            db.func.sum(db.case((ActiveErrand.status == 'completed', 1), else_=0)).label('completed_errands'),
            db.func.avg(Rating.rating).label('avg_rating')
        ).join(
            RunnerProfile, User.id == RunnerProfile.user_id
        ).outerjoin(
            ActiveErrand, User.id == ActiveErrand.runner_id
        ).outerjoin(
            Rating, User.id == Rating.to_user_id
        ).group_by(
            User.id, RunnerProfile.id
        ).all()

    runners_list = []
    for user_data, runner_profile, total_errands, completed_errands, avg_rating in runners_with_data:
        runner_data = {
            'user': {
                'id': user_data.id,
                'fullname': user_data.fullname,
                'username': user_data.username,
                'email': user_data.email
            },
            'runner_profile': runner_profile,
            'total_errands': total_errands or 0,
            'completed_errands': completed_errands or 0,
            'avg_rating': float(avg_rating) if avg_rating else 4.5
        }
        runners_list.append(runner_data)

    return render_template(
        "available_runners.html",
        runners=runners_list,
        errand=errand,
        user=user
    )


@app.route("/negotiate", methods=["POST"])
@login_required
def negotiate():
    user = current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 403

    errand_id = request.form.get("errand_id", type=int)
    runner_id = request.form.get("runner_id", type=int)
    offer_price = request.form.get("offer_price", type=float)

    errand = Errand.query.get(errand_id)
    if not errand:
        return jsonify({"error": "Errand not found"}), 404

    negotiation = Negotiation(
        errand_id=errand_id,
        runner_id=runner_id,
        offer_price=offer_price,
        status="pending"
    )
    db.session.add(negotiation)
    db.session.commit()

    create_notification(errand.client_id, f"New price offer received for your errand: ${offer_price}")

    return jsonify({"message": "Offer sent"}), 200


@app.route("/accept_offer", methods=["POST"])
@login_required
def accept_offer():
    user = current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 403

    negotiation_id = request.form.get("negotiation_id", type=int)
    negotiation = Negotiation.query.get(negotiation_id)
    if not negotiation:
        return jsonify({"error": "Negotiation not found"}), 404

    negotiation.status = "accepted"
    errand = Errand.query.get(negotiation.errand_id)
    errand.status = "accepted"
    active = ActiveErrand(
        errand_id=errand.id,
        runner_id=negotiation.runner_id,
        start_time=datetime.utcnow(),
        status="ongoing"
    )
    db.session.add(active)
    db.session.commit()

    create_notification(errand.client_id, f"Offer accepted! Your errand is now being processed by a runner.")
    create_notification(negotiation.runner_id,
                        f"Congratulations! Your offer was accepted. Start the errand: {errand.type}")

    return jsonify({"message": "Offer accepted"}), 200


@app.route("/complete_errand", methods=["POST"])
@login_required
def complete_errand():
    user = current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 403

    active_errand_id = request.form.get("active_errand_id", type=int)
    active_errand = ActiveErrand.query.get(active_errand_id)

    if not active_errand or active_errand.runner_id != user.id:
        return jsonify({"error": "Errand not found or unauthorized"}), 404

    active_errand.status = "completed"
    active_errand.end_time = datetime.utcnow()
    active_errand.errand.status = "completed"

    db.session.commit()

    create_notification(active_errand.errand.client_id,
                        f"Your errand '{active_errand.errand.type}' has been completed!")

    flash("Errand marked as completed!", "success")
    return redirect(url_for("runnerhome"))


@app.route("/Errandfinal.html")
@login_required
def errand_final():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type != "runner":
        flash("This page is for runners only", "warning")
        return redirect(url_for("home_page"))

    errand_id = request.args.get("errand_id", type=int)
    runner_price = request.args.get("runner_price", type=float)
    errand_title = request.args.get("errand_title", "")
    client_price = request.args.get("client_price", type=float)
    action = request.args.get("action", "")

    if not errand_id:
        flash("Errand ID is required", "danger")
        return redirect(url_for("runnerhome"))

    errand = Errand.query.get(errand_id)
    if not errand:
        flash("Errand not found", "danger")
        return redirect(url_for("runnerhome"))

    if action == "accepted":
        if errand.status != "pending":
            flash("This errand is no longer available", "warning")
            return redirect(url_for("runnerhome"))

        negotiation = Negotiation(
            errand_id=errand_id,
            runner_id=user.id,
            offer_price=runner_price or errand.price_estimate,
            status="accepted"
        )
        db.session.add(negotiation)

        errand.status = "accepted"

        active_errand = ActiveErrand(
            errand_id=errand_id,
            runner_id=user.id,
            start_time=datetime.utcnow(),
            status="ongoing"
        )
        db.session.add(active_errand)

        create_notification(errand.client_id, f"Your errand '{errand.type}' has been accepted by a runner!")
        create_notification(user.id, f"You have accepted the errand: {errand.type}")

        db.session.commit()

        flash("Errand accepted successfully! You can now start working on it.", "success")

    return render_template(
        "Errandfinal.html",
        user=user,
        errand=errand,
        runner_price=runner_price,
        errand_title=errand_title,
        client_price=client_price,
        action=action
    )


# ============================================================================
# PROFILE & SETTINGS ROUTES
# ============================================================================

@app.route("/settings")
@login_required
def settings():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    return render_template("settings.html", user=user)


@app.route("/profile")
@login_required
def profile():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    runner_profile = None
    if user.user_type == "runner":
        runner_profile = RunnerProfile.query.filter_by(user_id=user.id).first()

    return render_template("profile.html", user=user, runner_profile=runner_profile)


@app.route("/personal_info", methods=["GET", "POST"])
@login_required
def personal_info():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_personal_info":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()

            if email and not validate_email(email):
                flash("Please enter a valid email address", "danger")
                return redirect(url_for("personal_info"))

            if username and username != user.username:
                existing_user = User.query.filter_by(username=username).first()
                if existing_user and existing_user.id != user.id:
                    flash("Username is already taken", "danger")
                    return redirect(url_for("personal_info"))

            if email and email != user.email:
                existing_email = User.query.filter_by(email=email).first()
                if existing_email and existing_email.id != user.id:
                    flash("Email is already in use", "danger")
                    return redirect(url_for("personal_info"))

            if username:
                user.username = username
            if email:
                user.email = email
            if phone:
                if phone.startswith('+'):
                    user.phone = phone
                else:
                    if user.phone and user.phone.startswith('+'):
                        country_code = user.phone.split(' ')[0] if ' ' in user.phone else user.phone
                        user.phone = f"{country_code} {phone}"
                    else:
                        user.phone = f"+263 {phone}"

        elif action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not check_password_hash(user.password_hash, current_password):
                flash("Current password is incorrect", "danger")
                return redirect(url_for("personal_info"))

            if len(new_password) < 6:
                flash("New password must be at least 6 characters", "danger")
                return redirect(url_for("personal_info"))

            if new_password != confirm_password:
                flash("New passwords do not match", "danger")
                return redirect(url_for("personal_info"))

            user.password_hash = generate_password_hash(new_password)
            flash("Password updated successfully", "success")

        user.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Profile updated successfully", "success")
        return redirect(url_for("personal_info"))

    return render_template("personal_info.html", user=user)


@app.route("/Privacy")
@login_required
def privacy_security():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    return render_template("Privacy.html", user=user)


@app.route("/help")
@login_required
def help_support():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    return render_template("help.html", user=user)


@app.route("/rate", methods=["GET", "POST"])
@login_required
def rate_app():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    feedbacks = AppFeedback.query.all()
    total_ratings = len(feedbacks)
    avg_rating = sum(f.rating for f in feedbacks) / total_ratings if total_ratings > 0 else 4.8

    rating_dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for feedback in feedbacks:
        rating_dist[feedback.rating] += 1

    if total_ratings > 0:
        for i in range(1, 6):
            rating_dist[i] = (rating_dist[i] / total_ratings) * 100

    if request.method == "POST":
        rating = request.form.get("rating", type=int)
        feedback_type = request.form.get("feedback_type", "")
        feedback = request.form.get("feedback", "")
        suggestions = request.form.get("suggestions", "")
        contact_permission = request.form.get("contact_permission") == "on"

        if not rating or not 1 <= rating <= 5:
            flash("Please select a valid rating", "danger")
            return redirect(url_for("rate_app"))

        if not feedback or len(feedback.strip()) < 10:
            flash("Please provide meaningful feedback (at least 10 characters)", "danger")
            return redirect(url_for("rate_app"))

        app_feedback = AppFeedback(
            user_id=user.id,
            rating=rating,
            feedback_type=feedback_type,
            feedback=feedback.strip(),
            suggestions=suggestions.strip() if suggestions else None,
            contact_permission=contact_permission
        )
        db.session.add(app_feedback)
        db.session.commit()

        flash("Thank you for your feedback! We appreciate your help in improving ErrandGo.", "success")
        return redirect(url_for("rate_app"))

    return render_template("rate.html", user=user, avg_rating=round(avg_rating, 1),
                           total_ratings=total_ratings, rating_dist=rating_dist)


# ============================================================================
# STATIC & SUPPORT ROUTES
# ============================================================================

@app.route("/terms")
def terms():
    user = current_user()
    return render_template("terms.html", user=user, current_date=datetime.utcnow())


@app.route("/privacy")
def privacy():
    user = current_user()
    return render_template("privacy.html", user=user)


@app.route("/wallet")
@login_required
def wallet():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    return render_template("wallet.html", user=user)


@app.route("/map_view")
@login_required
def map_view():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    return render_template("map_view.html", user=user)


@app.route("/notifications")
@login_required
def notifications():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    user_notifications = Notification.query.filter_by(user_id=user.id).order_by(
        Notification.created_at.desc()).all()

    return render_template("notifications.html", user=user, notifications=user_notifications)


@app.route("/ratings")
@login_required
def ratings():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))

    if user.user_type == "runner":
        ratings_received = Rating.query.filter_by(to_user_id=user.id).order_by(
            Rating.created_at.desc()).all()
    else:
        ratings_received = Rating.query.filter_by(to_user_id=user.id).order_by(
            Rating.created_at.desc()).all()

    return render_template("ratings.html", user=user, ratings=ratings_received)


@app.route("/help")
@login_required
def help():
    user = current_user()
    if not user:
        return redirect(url_for("signin"))
    return render_template("help.html", user=user)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/errand/<int:errand_id>/details')
@login_required
def errand_details(errand_id):
    errand = Errand.query.get_or_404(errand_id)
    user = current_user()

    if user.id != errand.client_id and (user.user_type != 'runner' or user.id != errand.runner_id):
        return jsonify({'error': 'Unauthorized'}), 403

    runner = None
    if errand.runner_id:
        runner = User.query.get(errand.runner_id)

    active_errand = ActiveErrand.query.filter_by(errand_id=errand_id).first()

    ratings = Rating.query.filter_by(errand_id=errand_id).all()

    negotiation = Negotiation.query.filter_by(
        errand_id=errand_id,
        runner_id=runner.id if runner else None
    ).first()

    return jsonify({
        'id': errand.id,
        'type': errand.type,
        'status': errand.status,
        'pickup_location': errand.pickup_location,
        'delivery_location': errand.delivery_location,
        'weight': errand.weight,
        'delivery_time': errand.delivery_time,
        'details': errand.details,
        'price_estimate': float(errand.price_estimate) if errand.price_estimate else 0.0,
        'created_at': errand.created_at.isoformat() if errand.created_at else None,
        'client': {
            'id': errand.client.id,
            'fullname': errand.client.fullname,
            'email': errand.client.email
        },
        'runner': runner and {
            'id': runner.id,
            'fullname': runner.fullname,
            'email': runner.email
        },
        'active_errand': {
            'start_time': active_errand.start_time.isoformat() if active_errand and active_errand.start_time else None,
            'end_time': active_errand.end_time.isoformat() if active_errand and active_errand.end_time else None,
            'status': active_errand.status if active_errand else None
        } if active_errand else None,
        'negotiation': {
            'offer_price': float(negotiation.offer_price) if negotiation else 0.0,
            'status': negotiation.status if negotiation else None
        } if negotiation else None,
        'ratings': [{
            'rating': r.rating,
            'comment': r.comment,
            'from_user': User.query.get(r.from_user_id).fullname if User.query.get(r.from_user_id) else 'Unknown',
            'created_at': r.created_at.isoformat() if r.created_at else None
        } for r in ratings]
    })


@app.route('/api/rate-errand', methods=['POST'])
@login_required
def rate_errand():
    data = request.get_json()
    errand_id = data.get('errand_id')
    rating_value = data.get('rating')
    comment = data.get('comment', '')

    errand = Errand.query.get_or_404(errand_id)
    user = current_user()

    if user.id != errand.client_id:
        return jsonify({'success': False, 'error': 'Only clients can rate errands'}), 403

    if errand.status != 'completed':
        return jsonify({'success': False, 'error': 'Can only rate completed errands'}), 400

    active_errand = ActiveErrand.query.filter_by(errand_id=errand_id).first()
    if not active_errand:
        return jsonify({'success': False, 'error': 'Active errand not found'}), 404

    existing_rating = Rating.query.filter_by(
        errand_id=errand_id,
        from_user_id=user.id,
        to_user_id=active_errand.runner_id
    ).first()

    if existing_rating:
        existing_rating.rating = rating_value
        existing_rating.comment = comment
        existing_rating.created_at = datetime.utcnow()
    else:
        new_rating = Rating(
            errand_id=errand_id,
            from_user_id=user.id,
            to_user_id=active_errand.runner_id,
            rating=rating_value,
            comment=comment
        )
        db.session.add(new_rating)

    db.session.commit()

    return jsonify({'success': True})


@app.route('/errands/completed/filter', methods=['GET'])
@login_required
def filter_completed_errands():
    user = current_user()
    filter_type = request.args.get('filter', 'all')

    if user.user_type == 'client':
        base_query = Errand.query.filter(
            Errand.client_id == user.id,
            Errand.status == 'completed'
        )
    else:
        completed_active = ActiveErrand.query.filter_by(
            runner_id=user.id,
            status='completed'
        ).all()
        errand_ids = [ae.errand_id for ae in completed_active]
        base_query = Errand.query.filter(Errand.id.in_(errand_ids))

    now = datetime.utcnow()

    if filter_type == 'today':
        today = now.date()
        errands = base_query.filter(
            db.func.date(Errand.created_at) == today
        ).order_by(Errand.created_at.desc()).all()
    elif filter_type == 'week':
        week_ago = now - timedelta(days=7)
        errands = base_query.filter(
            Errand.created_at >= week_ago
        ).order_by(Errand.created_at.desc()).all()
    elif filter_type == 'month':
        month_ago = now - timedelta(days=30)
        errands = base_query.filter(
            Errand.created_at >= month_ago
        ).order_by(Errand.created_at.desc()).all()
    elif filter_type == 'high-earning':
        errands = base_query.order_by(Errand.price_estimate.desc()).all()
    else:
        errands = base_query.order_by(Errand.created_at.desc()).all()

    errands_data = []
    for errand in errands:
        runner = None
        if errand.runner_id:
            runner = User.query.get(errand.runner_id)

        rating = Rating.query.filter_by(
            errand_id=errand.id,
            from_user_id=user.id if user.user_type == 'client' else errand.client_id
        ).first()

        errands_data.append({
            'id': errand.id,
            'type': errand.type,
            'status': errand.status,
            'price_estimate': float(errand.price_estimate) if errand.price_estimate else 0.0,
            'created_at': errand.created_at.isoformat() if errand.created_at else None,
            'pickup_location': errand.pickup_location,
            'delivery_location': errand.delivery_location,
            'runner': runner.fullname if runner else 'Not assigned',
            'client': errand.client.fullname,
            'has_rating': rating is not None,
            'rating': rating.rating if rating else None
        })

    return jsonify({'errands': errands_data})


@app.route("/mark_notification_read/<int:notification_id>")
def mark_notification_read(notification_id):
    user = current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    notification = Notification.query.filter_by(id=notification_id, user_id=user.id).first()
    if notification:
        notification.is_read = True
        db.session.commit()
        return jsonify({"success": True})

    return jsonify({"error": "Notification not found"}), 404


@app.route("/rate_errand", methods=["POST"])
@login_required
def rate_errand_old():
    user = current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 403

    errand_id = request.form.get("errand_id", type=int)
    rating_value = request.form.get("rating", type=int)
    comment = request.form.get("comment", "").strip()

    errand = Errand.query.get(errand_id)
    if not errand:
        return jsonify({"error": "Errand not found"}), 404

    if user.user_type == "client" and user.id == errand.client_id:
        active_errand = ActiveErrand.query.filter_by(errand_id=errand_id).first()
        if not active_errand:
            return jsonify({"error": "Active errand not found"}), 404
        to_user_id = active_errand.runner_id
    elif user.user_type == "runner":
        active_errand = ActiveErrand.query.filter_by(errand_id=errand_id, runner_id=user.id).first()
        if not active_errand:
            return jsonify({"error": "Not authorized to rate this errand"}), 403
        to_user_id = errand.client_id
    else:
        return jsonify({"error": "Not authorized to rate this errand"}), 403

    existing_rating = Rating.query.filter_by(
        errand_id=errand_id,
        from_user_id=user.id
    ).first()

    if existing_rating:
        existing_rating.rating = rating_value
        existing_rating.comment = comment
        existing_rating.created_at = datetime.utcnow()
    else:
        rating = Rating(
            errand_id=errand_id,
            from_user_id=user.id,
            to_user_id=to_user_id,
            rating=rating_value,
            comment=comment
        )
        db.session.add(rating)

    db.session.commit()

    referrer = request.headers.get('Referer', '')
    if 'completed_errands' in referrer:
        flash("Thank you for your rating!", "success")
        return redirect(url_for("completed_errands"))
    else:
        flash("Thank you for your rating!", "success")
        return redirect(url_for("order_history"))


# ============================================================================
# UTILITY ROUTES
# ============================================================================

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/debug_user")
def debug_user():
    user = current_user()
    if user:
        return jsonify({
            "username": user.username,
            "user_type": user.user_type,
            "id": user.id,
            "session_user_type": session.get("user_type")
        })
    return jsonify({"error": "No user logged in"})


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('welcome.html'), 500


# ============================================================================
# MAIN APPLICATION ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=True)