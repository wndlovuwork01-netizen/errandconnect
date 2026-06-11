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
import threading
import time

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

def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_phone(phone):
    """
    Validate Zimbabwe mobile numbers.
    Must be 9 digits (after removing non-digits) starting with 71, 73, 77, or 78.
    """
    digits_only = re.sub(r'\D', '', phone)
    if len(digits_only) != 9:
        return False
    valid_prefixes = ['71', '73', '77', '78']
    if digits_only[:2] not in valid_prefixes:
        return False
    return digits_only.isdigit()

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def calculate_minimum_fee(distance_km, weight_kg, vehicle_type, current_time):
    fee_config = FeeConfig.query.first()
    if not fee_config:
        base_fee = 5.0; per_km = 1.5; per_kg = 0.5; night_mult = 1.5; rush_mult = 1.2
        vehicle_mults = {"foot": 1.0, "bike": 1.2, "motorcycle": 1.5, "car": 2.0, "truck": 3.0}
    else:
        base_fee = fee_config.base_fee; per_km = fee_config.per_km_fee; per_kg = fee_config.per_kg_fee
        night_mult = fee_config.night_multiplier; rush_mult = fee_config.rush_hour_multiplier
        vehicle_mults = json.loads(fee_config.vehicle_type_multiplier_json)
    try: weight = float(weight_kg)
    except: weight = 0
    fee = base_fee + (distance_km * per_km) + (weight * per_kg)
    hour = current_time.hour
    if 22 <= hour or hour <= 6: fee *= night_mult
    elif (7 <= hour <= 9) or (16 <= hour <= 18): fee *= rush_mult
    fee *= vehicle_mults.get(vehicle_type, 1.0)
    return round(fee, 2)

def get_first_form_value(form, keys):
    for key in keys:
        value = form.get(key)
        if value: return value.strip()
    return ""

def create_basic_errand(user, errand_type):
    form = request.form
    pickup_lat = form.get("pickup_lat") or form.get("pickup_latitude")
    pickup_lon = form.get("pickup_lon") or form.get("pickup_lng") or form.get("pickup_longitude")
    dropoff_lat = form.get("dropoff_lat") or form.get("dropoff_latitude")
    dropoff_lon = form.get("dropoff_lon") or form.get("dropoff_lng") or form.get("dropoff_longitude")
    distance = 0
    if pickup_lat and pickup_lon and dropoff_lat and dropoff_lon:
        try:
            distance = calculate_distance(float(pickup_lat), float(pickup_lon), float(dropoff_lat), float(dropoff_lon))
        except ValueError:
            distance = 0

    # ===== OVERRIDE WITH ROAD DISTANCE FROM CLIENT-SIDE MAP =====
    form_distance_km = form.get("distance_km")
    if form_distance_km:
        try:
            distance = float(form_distance_km)
        except ValueError:
            pass  # keep the straight-line distance if conversion fails

    weight_value = form.get("estimated_weight") or form.get("weight") or form.get("weight_kg") or form.get("package_weight") or "0"
    vehicle_type = form.get("vehicle_type") or "car"
    delivery_time = form.get("delivery_time") or form.get("delivery_timeframe") or form.get("collection_time") or form.get("specific_time") or ""
    pickup_location = get_first_form_value(form, [
        "pickup_location", "pickup_address", "store_location", "store_address",
        "restaurant_location", "collection_location", "collection_address",
        "service_location", "venue", "service_provider"
    ])
    delivery_location = get_first_form_value(form, [
        "delivery_location", "delivery_address", "dropoff_location", "dropoff_address",
        "destination", "to_location", "to_address"
    ])
    client_service_price = form.get("service_price")
    if client_service_price:
        try:
            client_offer = float(client_service_price)
        except ValueError:
            client_offer = calculate_minimum_fee(distance, weight_value, vehicle_type, datetime.now())
    else:
        client_offer = calculate_minimum_fee(distance, weight_value, vehicle_type, datetime.now())

    template_est_fee = form.get("estimated_fee")
    if template_est_fee:
        try:
            calculated_fee = float(template_est_fee)
            if calculated_fee <= 0:
                calculated_fee = calculate_minimum_fee(distance, weight_value, vehicle_type, datetime.now())
        except ValueError:
            calculated_fee = calculate_minimum_fee(distance, weight_value, vehicle_type, datetime.now())
    else:
        calculated_fee = calculate_minimum_fee(distance, weight_value, vehicle_type, datetime.now())

    details = form.to_dict(flat=False)
    now = datetime.utcnow()
    errand = Errand(
        client_id=user.id,
        type=errand_type,
        pickup_location=pickup_location,
        delivery_location=delivery_location,
        weight=weight_value,
        delivery_time=delivery_time,
        distance_km=distance,
        details=json.dumps(details),
        price_estimate=client_offer,
        calculated_minimum_fee=calculated_fee,
        status="available",
        expires_at=now + timedelta(minutes=5),
        hard_deadline=now + timedelta(minutes=7)
    )
    db.session.add(errand)
    db.session.commit()
    return errand

@app.route("/uploads/<path:filename>")
def uploaded_file(filename): return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

def get_available_errands_count(user_id):
    rp = RunnerProfile.query.filter_by(user_id=user_id).first()
    rc = getattr(rp, 'city', '')
    if rc: return Errand.query.filter(Errand.status.in_(['available','pending']), Errand.pickup_location.ilike(f"%{rc}%")).count()
    return Errand.query.filter(Errand.status.in_(['available','pending'])).count()

def serialize_user(user):
    if not user: return None
    return {"id":user.id,"fullname":user.fullname,"username":user.username,"email":user.email,"phone":user.phone,"average_rating":user.average_rating}

def serialize_runner_profile(profile):
    if not profile: return None
    piu = url_for("uploaded_file",filename=profile.profile_photo) if profile.profile_photo else None
    return {"id":profile.id,"user_id":profile.user_id,"full_name":profile.full_name,"phone_number":profile.phone_number,"profile_image":profile.profile_photo,"profile_photo":profile.profile_photo,"profile_image_url":piu,"city":profile.city,"vehicle_type":profile.vehicle_type,"is_available":profile.is_available,"current_latitude":profile.current_latitude,"current_longitude":profile.current_longitude}

def serialize_errand(errand):
    if not errand: return None
    ca = errand.created_at.isoformat() if errand.created_at else None
    ea = None; hd = None
    if hasattr(errand,'expires_at') and errand.expires_at: ea = errand.expires_at.isoformat()
    if hasattr(errand,'hard_deadline') and errand.hard_deadline: hd = errand.hard_deadline.isoformat()
    return {"id":errand.id,"type":errand.type,"pickup_location":errand.pickup_location,"delivery_location":errand.delivery_location,"weight":errand.weight,"delivery_time":errand.delivery_time,"details":errand.details,"price_estimate":errand.price_estimate,"calculated_minimum_fee":getattr(errand,'calculated_minimum_fee',errand.price_estimate),"distance_km":getattr(errand,'distance_km',None),"status":errand.status,"created_at":ca,"expires_at":ea,"hard_deadline":hd}
def get_or_create_support_chat(user):
    """Ensure every user has a support chat by using a dedicated Support errand."""
    # 1) Find or create the special "Support" errand (only once per app)
    support_errand = Errand.query.filter_by(type="System Support").first()
    if not support_errand:
        support_errand = Errand(
            client_id=user.id,  # temporary, will be overwritten later but harmless
            type="System Support",
            pickup_location="Internal",
            delivery_location="Internal",
            weight="0",
            delivery_time="N/A",
            distance_km=0,
            details=json.dumps({"note": "Automated support chat"}),
            price_estimate=0,
            calculated_minimum_fee=0,
            status="system",
            expires_at=datetime.utcnow() + timedelta(days=365),
            hard_deadline=datetime.utcnow() + timedelta(days=365)
        )
        db.session.add(support_errand)
        db.session.commit()

    # 2) Find or create the support user
    support_user = User.query.filter_by(email="support@errandgo.com").first()
    if not support_user:
        support_user = User(
            fullname="ErrandGo Support",
            username="support",
            email="support@errandgo.com",
            password_hash=generate_password_hash("support"),
            user_type="client"
        )
        db.session.add(support_user)
        db.session.commit()

    # 3) Create a chat between the current user and the support user, linked to the support errand
    if user.user_type == "client":
        chat = Chat.query.filter_by(client_id=user.id, runner_id=support_user.id, errand_id=support_errand.id).first()
        if not chat:
            chat = Chat(errand_id=support_errand.id, client_id=user.id, runner_id=support_user.id)
            db.session.add(chat)
            db.session.commit()
    else:
        chat = Chat.query.filter_by(client_id=support_user.id, runner_id=user.id, errand_id=support_errand.id).first()
        if not chat:
            chat = Chat(errand_id=support_errand.id, client_id=support_user.id, runner_id=user.id)
            db.session.add(chat)
            db.session.commit()

    return chat

@app.template_filter('timesince')
def timesince_filter(dt):
    if not dt: return ""
    if isinstance(dt,str):
        try: dt = datetime.fromisoformat(dt.replace('Z','+00:00'))
        except ValueError: return dt
    now = datetime.now()
    if dt.tzinfo is not None: now = datetime.now(dt.tzinfo)
    diff = now - dt
    periods = ((diff.days//365,"year","years"),(diff.days//30,"month","months"),(diff.days//7,"week","weeks"),(diff.days,"day","days"),(diff.seconds//3600,"hour","hours"),(diff.seconds//60,"minute","minutes"),(diff.seconds,"second","seconds"))
    for count,singular,plural in periods:
        if count >= 1: return f"{count} {singular if count==1 else plural} ago"
    return "just now"

# ============================================================================
# CORE ROUTES
# ============================================================================
@app.route("/")
def index():
    if current_user(): return redirect(url_for('home_page'))
    return render_template("index.html")

@app.route("/signin", methods=["GET","POST"])
def signin():
    if request.method=="POST":
        identifier=request.form.get("identifier") or request.form.get("username") or request.form.get("email")
        password=request.form.get("password")
        user=User.query.filter((User.email==identifier)|(User.username==identifier)).first()
        if user and check_password_hash(user.password_hash,password):
            session['user_id']=user.id
            if user.user_type=="runner": return redirect(url_for('runnerhome'))
            return redirect(url_for('home_page'))
        flash("Invalid credentials","danger")
    return render_template("signin.html")


@app.route("/logout")
def logout():
    session.pop('user_id',None)
    return redirect(url_for('signin'))

@app.route("/home")
@login_required
def home_page():
    user=current_user()
    if user.user_type=="runner": return redirect(url_for('runnerhome'))
    return render_template("home.html",user=user,pending_count=Errand.query.filter_by(client_id=user.id,status="pending").count(),completed_count=Errand.query.filter_by(client_id=user.id,status="completed").count())

# ==================== UPDATED ROUTE ====================
@app.route("/runnerhome")
@login_required
def runnerhome():
    user=current_user()
    if user.user_type!="runner": return redirect(url_for('home_page'))
    rp=RunnerProfile.query.filter_by(user_id=user.id).first()
    if not rp: flash("Complete profile","info"); return redirect(url_for('runner_register'))
    completed=ActiveErrand.query.filter_by(runner_id=user.id,status="completed").count()
    active=ActiveErrand.query.filter_by(runner_id=user.id,status="ongoing").count()
    rc=getattr(rp,'city','')
    now_utc = datetime.utcnow()
    if rc:
        ae=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc,
            Errand.pickup_location.ilike(f"%{rc}%")
        ).all()
    else:
        ae=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc
        ).all()
    total=0.0
    for a in ActiveErrand.query.filter_by(runner_id=user.id,status="completed").all():
        n=Negotiation.query.filter_by(errand_id=a.errand_id,runner_id=user.id,status="accepted").first()
        total+=n.offer_price if n else a.errand.price_estimate
    return render_template("runnerhome.html",user=user,completed_count=completed,pending_count=active,available_errands=ae,total_earnings=total)

@app.route("/runnerprofile")
@login_required
def runnerprofile():
    user=current_user()
    if not user: return redirect(url_for("signin"))
    if user.user_type!="runner": flash("Runners only","warning"); return redirect(url_for("home_page"))
    rp=RunnerProfile.query.filter_by(user_id=user.id).first()
    ce=ActiveErrand.query.filter_by(runner_id=user.id,status="completed").all()
    ae=ActiveErrand.query.filter_by(runner_id=user.id,status="ongoing").all()
    te=0.0
    for a in ce:
        n=Negotiation.query.filter_by(errand_id=a.errand_id,runner_id=user.id,status="accepted").first()
        te+=n.offer_price if n else a.errand.price_estimate
    ratings=Rating.query.filter_by(to_user_id=user.id).all()
    avg=sum(r.rating for r in ratings)/len(ratings) if ratings else 0
    return render_template("runnerprofile.html",user=user,runner_profile=rp,completed_count=len(ce),active_count=len(ae),total_earnings=te,avg_rating=avg)

# ==================== UPDATED ROUTE ====================
@app.route("/dashboardrunner")
@login_required
def dashboardrunner():
    user=current_user()
    if user.user_type!="runner": return redirect(url_for('home_page'))
    cc=ActiveErrand.query.filter_by(runner_id=user.id,status="completed").count()
    ac=ActiveErrand.query.filter_by(runner_id=user.id,status="ongoing").count()
    today=datetime.utcnow().date()
    te=sum(e.errand.price_estimate for e in ActiveErrand.query.filter(ActiveErrand.runner_id==user.id,ActiveErrand.status=="completed",db.func.date(ActiveErrand.end_time)==today).all())
    rp=RunnerProfile.query.filter_by(user_id=user.id).first()
    rc=getattr(rp,'city','')
    now_utc = datetime.utcnow()
    if rc:
        ae=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc,
            Errand.pickup_location.ilike(f"%{rc}%")
        ).all()
    else:
        ae=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc
        ).all()
    notifs=Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(5).all()
    ratings=Rating.query.filter_by(to_user_id=user.id).all()
    avg=sum(r.rating for r in ratings)/len(ratings) if ratings else 0
    weekly=[]
    for i in range(7):
        d=today-timedelta(days=6-i)
        de=ActiveErrand.query.filter(ActiveErrand.runner_id==user.id,ActiveErrand.status=="completed",db.func.date(ActiveErrand.end_time)==d).all()
        weekly.append({"day":d.strftime('%a'),"earnings":sum(e.errand.price_estimate for e in de)})
    return render_template("runnerdashboard.html",user=user,available_errands=ae,completed_count=cc,active_count=ac,today_earnings=te,notifications=notifs,weekly_earnings=weekly,avg_rating=avg)

@app.route("/runnercompleted")
@login_required
def runnercompleted():
    user=current_user()
    if user.user_type!="runner": return redirect(url_for('home_page'))
    return render_template("runnercompleted.html",user=user,completed_errands=ActiveErrand.query.filter_by(runner_id=user.id,status="completed").all())

# ==================== UPDATED ROUTE ====================
@app.route("/runneravailable_errands")
@login_required
def runneravailable_errands():
    user=current_user()
    if user.user_type!="runner": return redirect(url_for('home_page'))
    rp=RunnerProfile.query.filter_by(user_id=user.id).first()
    rc=getattr(rp,'city','')
    now_utc = datetime.utcnow()
    if rc:
        ae=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc,
            Errand.pickup_location.ilike(f"%{rc}%")
        ).all()
    else:
        ae=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc
        ).all()
    data=[{"errand":serialize_errand(e),"client":serialize_user(e.client)} for e in ae]
    return render_template("runneravailable_errands.html",user=user,available_errands=data)

# ==================== UPDATED ROUTE ====================
@app.route("/api/errands")
@login_required
def api_available_errands():
    user=current_user()
    if user.user_type!="runner": return jsonify([])
    rp=RunnerProfile.query.filter_by(user_id=user.id).first()
    rc=getattr(rp,'city','')
    now_utc = datetime.utcnow()
    if rc:
        q=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc,
            Errand.pickup_location.ilike(f"%{rc}%")
        )
    else:
        q=Errand.query.filter(
            Errand.status.in_(['available','pending']),
            Errand.hard_deadline > now_utc
        )
    return jsonify([{"errand":serialize_errand(e),"client":serialize_user(e.client)} for e in q.order_by(Errand.created_at.desc()).all()])

@app.route("/runnerhistory")
@login_required
def runnerhistory():
    user=current_user()
    if user.user_type!="runner": return redirect(url_for('home_page'))
    ae=ActiveErrand.query.filter_by(runner_id=user.id).order_by(ActiveErrand.id.desc()).all()
    return render_template("runnerhistory.html",user=user,orders=ae,total_orders=len(ae),total_amount=sum(a.errand.price_estimate for a in ae if a.status=="completed"),completed_orders=len([a for a in ae if a.status=="completed"]))

@app.route("/runnerwallet")
@login_required
def runnerwallet():
    user=current_user()
    if user.user_type!="runner": return redirect(url_for('home_page'))
    ce=ActiveErrand.query.filter_by(runner_id=user.id,status="completed").all()
    tb=sum(e.errand.price_estimate for e in ce)
    return render_template("runnerwallet.html",user=user,total_balance=tb,available_balance=tb,pending_balance=0.0,transactions=[])

@app.route("/dashboard")
@login_required
def dashboard():
    if current_user().user_type=="runner": return redirect(url_for('runnerhome'))
    return render_template("dashboard_client.html",user=current_user())

@app.route("/order_history")
@login_required
def order_history():
    user=current_user()
    orders=Errand.query.filter_by(client_id=user.id).order_by(Errand.created_at.desc()).all()
    return render_template("order_history.html",user=user,orders=orders,total_orders=len(orders),pending_count=Errand.query.filter_by(client_id=user.id,status="pending").count(),completed_count=Errand.query.filter_by(client_id=user.id,status="completed").count(),now=datetime.utcnow())

@app.route("/settings")
@login_required
def settings(): return render_template("settings.html",user=current_user())
@app.route("/map_view")
@login_required
def map_view(): return render_template("map_view.html",user=current_user())
@app.route("/notifications")
@login_required
def notifications():
    return render_template("notifications.html",user=current_user(),notifications=Notification.query.filter_by(user_id=current_user().id).order_by(Notification.created_at.desc()).all())
@app.route("/ratings")
@login_required
def ratings(): return render_template("ratings.html",user=current_user(),ratings=Rating.query.filter_by(to_user_id=current_user().id).all())
@app.route("/profile")
@login_required
def profile(): return render_template("profile.html",user=current_user())
@app.route("/wallet")
@login_required
def wallet(): return render_template("wallet.html",user=current_user())
@app.route("/completed")
@login_required
def completed():
    user=current_user()
    e=Errand.query.filter_by(client_id=user.id,status="completed").first()
    r=Rating.query.filter_by(client_id=user.id,errand_id=e.id).first() if e else None
    return render_template("completed.html",user=user,errand=e,user_rating={"found":r is not None,"rating":r.value if r else 0},average_rating=sum(r.value for r in (Rating.query.filter_by(errand_id=e.id).all() if e else[]))/max(len(Rating.query.filter_by(errand_id=e.id).all() if e else[]),1))
@app.route("/terms")
@login_required
def terms(): return render_template("terms.html",user=current_user(),current_date=datetime.utcnow())
@app.route("/runner/terms")
def runnerterms(): return render_template("runnerterms.html")
@app.route("/privacy")
@login_required
def privacy(): return render_template("Privacy.html",user=current_user())
@app.route("/help")
@login_required
def help_support(): return render_template("help.html",user=current_user())
@app.route("/personal_info")
@login_required
def personal_info(): return render_template("personal_info.html",user=current_user())
@app.route("/privacy_security")
@login_required
def privacy_security(): return render_template("Privacy.html",user=current_user())
@app.route("/rate_app")
@login_required
def rate_app(): return render_template("rate.html",user=current_user())

# ============================================================================
# ERRAND ROUTES
# ============================================================================
@app.route("/create_grocery_errand", methods=["GET","POST"])
@login_required
def create_grocery_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Grocery")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("grocery.html",user=current_user())

@app.route("/create_food_delivery_errand", methods=["GET","POST"])
@login_required
def create_food_delivery_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Food Delivery")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("food_delivery.html",user=current_user())

@app.route("/create_bill_payment_errand", methods=["GET","POST"])
@login_required
def create_bill_payment_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Bill Payment")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("bill_payments.html",user=current_user())

@app.route("/create_package_delivery_errand", methods=["GET","POST"])
@login_required
def create_package_delivery_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Package Delivery")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("package_delivery.html",user=current_user())

@app.route("/create_gadget_service_errand", methods=["GET","POST"])
@login_required
def create_gadget_service_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Gadget Service")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("gadget_service.html",user=current_user())

@app.route("/create_collections_errand", methods=["GET","POST"])
@login_required
def create_collections_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Collections")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("Collections.html",user=current_user())

@app.route("/create_ticket_booking_errand", methods=["GET","POST"])
@login_required
def create_ticket_booking_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Ticket Booking")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("ticket_booking.html",user=current_user())

@app.route("/create_spare_parts_errand", methods=["GET","POST"])
@login_required
def create_spare_parts_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Spare Parts")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("spare_parts.html",user=current_user())

@app.route("/create_gas_delivery_errand", methods=["GET","POST"])
@login_required
def create_gas_delivery_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Gas Delivery")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("gas_delivery.html",user=current_user())

@app.route("/create_other_service_errand", methods=["GET","POST"])
@login_required
def create_other_service_errand():
    if request.method=="POST":
        errand=create_basic_errand(current_user(),"Other Service")
        return redirect(url_for('available_runners',errand_id=errand.id))
    return render_template("other.html",user=current_user())

@app.route("/purchase_page")
@login_required
def purchase_page(): return render_template("purchase.html",user=current_user())
@app.route("/property_page")
@login_required
def property_page(): return render_template("property.html",user=current_user())

@app.route("/create_purchase_errand", methods=["POST"])
@login_required
def create_purchase_errand():
    user = current_user()
    now = datetime.utcnow()
    store_name = request.form.get("store_name")
    store_location = request.form.get("store_location")
    delivery_address = request.form.get("delivery_address")
    delivery_time = request.form.get("delivery_time")
    specific_time = request.form.get("specific_time")
    estimated_weight = request.form.get("estimated_weight")
    pickup_lat = request.form.get("pickup_lat")
    pickup_lon = request.form.get("pickup_lon")
    dropoff_lat = request.form.get("dropoff_lat")
    dropoff_lon = request.form.get("dropoff_lon")

    items = []
    for i, n in enumerate(request.form.getlist("items[]")):
        if (n or "").strip():
            items.append({
                "name": n.strip(),
                "quantity": request.form.getlist("quantities[]")[i] if i < len(request.form.getlist("quantities[]")) else "",
                "brand": request.form.getlist("brands[]")[i] if i < len(request.form.getlist("brands[]")) else "",
                "price": request.form.getlist("prices[]")[i] if i < len(request.form.getlist("prices[]")) else ""
            })

    # Calculate straight-line distance first (fallback)
    distance = 0
    if pickup_lat and pickup_lon and dropoff_lat and dropoff_lon:
        try:
            distance = calculate_distance(float(pickup_lat), float(pickup_lon),
                                         float(dropoff_lat), float(dropoff_lon))
        except ValueError:
            distance = 0

    # ===== OVERRIDE WITH ROAD DISTANCE FROM CLIENT-SIDE MAP =====
    form_distance_km = request.form.get("distance_km")
    if form_distance_km:
        try:
            distance = float(form_distance_km)
        except ValueError:
            pass  # keep straight-line distance if conversion fails

    fee = calculate_minimum_fee(distance, estimated_weight or "0", "car", datetime.now())
    st = specific_time if delivery_time == "specific" and specific_time else delivery_time

    # Client's offered price
    sp = request.form.get("service_price")
    try:
        co = float(sp) if sp else fee
    except ValueError:
        co = fee

    # System estimated fee from template
    ef = request.form.get("estimated_fee")
    try:
        cf = float(ef) if ef else fee
    except ValueError:
        cf = fee

    errand = Errand(
        client_id=user.id,
        type="Purchase",
        pickup_location=store_location,
        delivery_location=delivery_address,
        weight=estimated_weight or "0",
        delivery_time=st,
        distance_km=distance,
        details=json.dumps({
            "store_name": store_name,
            "store_location": store_location,
            "delivery_address": delivery_address,
            "delivery_time": delivery_time,
            "specific_time": specific_time,
            "items": items
        }),
        price_estimate=co,
        calculated_minimum_fee=cf,
        status="available",
        expires_at=now + timedelta(minutes=5),
        hard_deadline=now + timedelta(minutes=7)
    )
    db.session.add(errand)
    db.session.commit()
    return redirect(url_for('available_runners', errand_id=errand.id))

@app.route("/create_property_errand", methods=["POST"])
@login_required
def create_property_errand():
    user = current_user()
    now = datetime.utcnow()
    store_name = request.form.get("store_name")
    store_location = request.form.get("store_location")
    collection_location = request.form.get("collection_location")
    delivery_address = request.form.get("delivery_address")
    delivery_time = request.form.get("delivery_time")
    specific_time = request.form.get("specific_time")
    estimated_weight = request.form.get("estimated_weight")
    pickup_lat = request.form.get("pickup_lat")
    pickup_lon = request.form.get("pickup_lon")
    dropoff_lat = request.form.get("dropoff_lat")
    dropoff_lon = request.form.get("dropoff_lon")

    items = []
    for i, n in enumerate(request.form.getlist("items[]")):
        if (n or "").strip():
            items.append({
                "name": n.strip(),
                "quantity": request.form.getlist("quantities[]")[i] if i < len(request.form.getlist("quantities[]")) else "",
                "brand": request.form.getlist("brands[]")[i] if i < len(request.form.getlist("brands[]")) else "",
                "price": request.form.getlist("prices[]")[i] if i < len(request.form.getlist("prices[]")) else ""
            })

    stype = "collect-deliver" if collection_location else "buy-deliver"
    ploc = store_location if stype == "buy-deliver" else collection_location

    # Calculate straight-line distance (fallback)
    distance = 0
    if pickup_lat and pickup_lon and dropoff_lat and dropoff_lon:
        try:
            distance = calculate_distance(float(pickup_lat), float(pickup_lon),
                                         float(dropoff_lat), float(dropoff_lon))
        except ValueError:
            distance = 0

    # ===== OVERRIDE WITH ROAD DISTANCE FROM CLIENT-SIDE MAP =====
    form_distance_km = request.form.get("distance_km")
    if form_distance_km:
        try:
            distance = float(form_distance_km)
        except ValueError:
            pass

    fee = calculate_minimum_fee(distance, estimated_weight or "0", "car", datetime.now())
    st = specific_time if delivery_time == "specific" and specific_time else delivery_time

    # Client's offer
    sp = request.form.get("service_price")
    try:
        co = float(sp) if sp else fee
    except ValueError:
        co = fee

    # System estimated fee from template
    ef = request.form.get("estimated_fee")
    try:
        cf = float(ef) if ef else fee
    except ValueError:
        cf = fee

    errand = Errand(
        client_id=user.id,
        type="Property",
        pickup_location=ploc,
        delivery_location=delivery_address,
        weight=estimated_weight or "0",
        delivery_time=st,
        distance_km=distance,
        details=json.dumps({
            "service_type": stype,
            "store_name": store_name,
            "store_location": store_location,
            "collection_location": collection_location,
            "delivery_address": delivery_address,
            "delivery_time": delivery_time,
            "specific_time": specific_time,
            "items": items
        }),
        price_estimate=co,
        calculated_minimum_fee=cf,
        status="available",
        expires_at=now + timedelta(minutes=5),
        hard_deadline=now + timedelta(minutes=7)
    )
    db.session.add(errand)
    db.session.commit()
    return redirect(url_for('available_runners', errand_id=errand.id))

@app.route("/create_errand", methods=["GET","POST"])
@login_required
def create_errand():
    user = current_user()
    if request.method == "POST":
        now = datetime.utcnow()
        pickup = request.form.get("pickup_location")
        dropoff = request.form.get("delivery_location")
        details = request.form.get("details")
        vehicle_type = request.form.get("vehicle_type", "car")
        weight = request.form.get("weight", "0")
        pickup_lat = request.form.get("pickup_lat")
        pickup_lon = request.form.get("pickup_lon")
        dropoff_lat = request.form.get("dropoff_lat")
        dropoff_lon = request.form.get("dropoff_lon")

        # Calculate straight-line distance (fallback)
        distance = 0
        if pickup_lat and pickup_lon and dropoff_lat and dropoff_lon:
            try:
                distance = calculate_distance(float(pickup_lat), float(pickup_lon),
                                             float(dropoff_lat), float(dropoff_lon))
            except ValueError:
                distance = 0

        # ===== OVERRIDE WITH ROAD DISTANCE FROM CLIENT-SIDE MAP =====
        form_distance_km = request.form.get("distance_km")
        if form_distance_km:
            try:
                distance = float(form_distance_km)
            except ValueError:
                pass

        fee = calculate_minimum_fee(distance, weight, vehicle_type, datetime.now())

        errand = Errand(
            client_id=user.id,
            type="General",
            pickup_location=pickup,
            delivery_location=dropoff,
            distance_km=distance,
            weight_kg=weight,
            details=details,
            price_estimate=fee,
            calculated_minimum_fee=fee,
            status="available",
            expires_at=now + timedelta(minutes=5),
            hard_deadline=now + timedelta(minutes=7)
        )
        db.session.add(errand)
        db.session.commit()
        return redirect(url_for('available_runners', errand_id=errand.id))

    return render_template("create_errand.html", user=user)

@app.route("/available_runners/<int:errand_id>")
@login_required
def available_runners(errand_id):
    user = current_user()
    errand = Errand.query.get_or_404(errand_id)
    if hasattr(errand, 'status') and errand.status == 'pending' and errand.client_id == user.id:
        now = datetime.utcnow()
        errand.status = 'available'
        if hasattr(errand, 'expires_at'): errand.expires_at = now + timedelta(minutes=5)
        if hasattr(errand, 'hard_deadline'): errand.hard_deadline = now + timedelta(minutes=7)
        db.session.commit()
    runners = RunnerProfile.query.filter_by(is_available=True).all()
    rd = [{"user": serialize_user(p.user), "runner_profile": serialize_runner_profile(p),
           "avg_rating": p.user.average_rating if p.user else 0,
           "completed_errands": ActiveErrand.query.filter_by(runner_id=p.user_id, status="completed").count(),
           "total_errands": ActiveErrand.query.filter_by(runner_id=p.user_id).count()} for p in runners]

    # Build a dict of runner_id -> offer_price for this errand
    bids = {}
    for neg in Negotiation.query.filter_by(errand_id=errand_id).all():
        if neg.offer_price and neg.offer_price > 0:
            bids[str(neg.runner_id)] = float(neg.offer_price)

    return render_template("available_runners.html", user=user, errand=errand, runners=rd,
                           client_offer=errand.price_estimate,
                           est_fee=errand.calculated_minimum_fee or errand.price_estimate, runner_bids=bids)
# ============================================================================
# RUNNER SETTINGS ROUTES
# ============================================================================
@app.route("/runnersettings")
@login_required
def runnersettings():
    if current_user().user_type!="runner": flash("Runners only","warning"); return redirect(url_for("home_page"))
    return render_template("runnersettings.html",user=current_user())
@app.route("/runnerpersonal", methods=["GET","POST"])
@login_required
def runnerpersonal():
    user=current_user()
    if user.user_type!="runner": flash("Runners only","warning"); return redirect(url_for("home_page"))
    if request.method=="POST":
        if request.form.get("email"): user.email=request.form.get("email")
        if request.form.get("phone"): user.phone=request.form.get("phone")
        db.session.commit(); flash("Updated.","success"); return redirect(url_for("runnerpersonal"))
    return render_template("runnerpersonal.html",user=user,runner_profile=RunnerProfile.query.filter_by(user_id=user.id).first())
@app.route("/runnerbank", methods=["GET","POST"])
@login_required
def runnerbank():
    user=current_user()
    if user.user_type!="runner": flash("Runners only","warning"); return redirect(url_for("home_page"))
    if request.method=="POST":
        if request.form.get("action")=="buy_package": flash(f"Purchased {request.form.get('errands_bought',0)}.","success"); return redirect(url_for("runnerbank"))
        flash("Saved.","success"); return redirect(url_for("runnerbank"))
    return render_template("runnerbank.html",user=user,remaining_errands=getattr(user,'errands',5),runner_profile=RunnerProfile.query.filter_by(user_id=user.id).first())
@app.route("/runnerpasswords", methods=["GET","POST"])
@login_required
def runnerpasswords():
    user=current_user()
    if request.method=="POST":
        cp=request.form.get("current_password"); np=request.form.get("new_password")
        if not check_password_hash(user.password_hash,cp): flash("Incorrect.","danger")
        elif np!=request.form.get("confirm_password"): flash("No match.","danger")
        elif len(np)<6: flash("Too short.","danger")
        else: user.password_hash=generate_password_hash(np); db.session.commit(); flash("Updated.","success"); return redirect(url_for("runnerpasswords"))
    return render_template("runnerpasswords.html",user=user)
@app.route("/runnerprivacy")
@login_required
def runnerprivacy(): return render_template("runnerprivacy.html",user=current_user())
@app.route("/runnerhelp", methods=["GET","POST"])
@login_required
def runnerhelp():
    if request.method=="POST": flash("Sent.","success"); return redirect(url_for("runnerhelp"))
    return render_template("runnerhelp.html",user=current_user())
@app.route("/runnerguideline")
@login_required
def runnerguideline(): return render_template("runnerguideline.html",user=current_user())
@app.route("/runnerfaqs")
@login_required
def runnerfaqs(): return render_template("runnerfaqs.html",user=current_user())
@app.route("/runnerrate", methods=["GET","POST"])
@login_required
def runnerrate():
    if request.method=="POST": flash("Thanks!","success"); return redirect(url_for("runnerrate"))
    return render_template("runnerrate.html",user=current_user())

# ============================================================================
# CHAT & TRACKING ROUTES
# ============================================================================
@app.route("/chats")
@login_required
def chats():
    user = current_user()
    support_chat = get_or_create_support_chat(user)

    if user.user_type == "client":
        ucs = Chat.query.filter_by(client_id=user.id).order_by(Chat.created_at.desc()).all()
    else:
        ucs = Chat.query.filter_by(runner_id=user.id).order_by(Chat.created_at.desc()).all()

    return render_template("chats.html", user=user, chats=ucs, support_chat_id=support_chat.id)

@app.route("/chat/<int:chat_id>")
@login_required
def chat_detail(chat_id):
    user = current_user()
    chat = Chat.query.get_or_404(chat_id)
    if user.id != chat.client_id and user.id != chat.runner_id:
        flash("Unauthorized", "danger")
        return redirect(url_for("home_page"))

    for msg in Message.query.filter_by(chat_id=chat.id, is_read=False).all():
        if msg.sender_id != user.id:
            msg.is_read = True
    db.session.commit()

    agreed_price = None
    neg = Negotiation.query.filter_by(errand_id=chat.errand_id, status="accepted").first()
    if neg and neg.offer_price > 0:
        agreed_price = neg.offer_price

    support_chat = get_or_create_support_chat(user)
    
    if user.user_type == "client":
        ucs = Chat.query.filter_by(client_id=user.id).order_by(Chat.created_at.desc()).all()
    else:
        ucs = Chat.query.filter_by(runner_id=user.id).order_by(Chat.created_at.desc()).all()

    messages = Message.query.filter_by(chat_id=chat.id).order_by(Message.created_at.asc()).all()
    active_errand = ActiveErrand.query.filter_by(errand_id=chat.errand_id).first()

    return render_template("chats.html",
                           user=user,
                           active_chat=chat,
                           chats=ucs,
                           messages=messages,
                           active_errand=active_errand,
                           agreed_price=agreed_price,
                           support_chat_id=support_chat.id)

@app.route("/api/send_message", methods=["POST"])
@login_required
def send_message():
    user=current_user(); data=request.json
    if not data.get("chat_id") or not data.get("content"): return jsonify({"error":"Missing"}),400
    chat=Chat.query.get_or_404(data["chat_id"])
    if user.id!=chat.client_id and user.id!=chat.runner_id: return jsonify({"error":"Unauthorized"}),403
    msg=Message(chat_id=chat.id,sender_id=user.id,content=data["content"])
    db.session.add(msg); db.session.commit()
    return jsonify({"success":True,"message":{"id":msg.id,"content":msg.content,"sender_id":msg.sender_id,"created_at":msg.created_at.strftime("%H:%M")}})

@app.route("/api/get_messages", methods=["GET"])
@login_required
def get_messages():
    chat_id = request.args.get("chat_id")
    after = request.args.get("after", 0, type=int)
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    chat = Chat.query.get(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    if current_user().id not in (chat.client_id, chat.runner_id):
        return jsonify({"error": "Unauthorized"}), 403

    messages = Message.query.filter(
        Message.chat_id == chat_id,
        Message.id > after
    ).order_by(Message.id.asc()).all()

    return jsonify({
        "messages": [{
            "id": msg.id,
            "content": msg.content,
            "sender_id": msg.sender_id,
            "created_at": msg.created_at.strftime("%H:%M")
        } for msg in messages]
    })

@app.route("/api/send_voice_message", methods=["POST"])
@login_required
def send_voice_message():
    chat_id = request.form.get("chat_id")
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    chat = Chat.query.get(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    if current_user().id not in (chat.client_id, chat.runner_id):
        return jsonify({"error": "Unauthorized"}), 403

    file = request.files.get("audio")
    if not file or file.filename == "":
        return jsonify({"error": "No audio file"}), 400

    filename = secure_filename(f"voice_{current_user().id}_{int(time.time())}.webm")
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    # Create a message that embeds an audio player
    audio_url = url_for("uploaded_file", filename=filename)
    msg_content = f'<audio controls src="{audio_url}"></audio>'

    msg = Message(chat_id=chat_id, sender_id=current_user().id, content=msg_content)
    db.session.add(msg)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": {
            "id": msg.id,
            "content": msg.content,
            "sender_id": msg.sender_id,
            "created_at": msg.created_at.strftime("%H:%M")
        }
    })

@app.route("/api/update_tracking", methods=["POST"])
@login_required
def update_tracking():
    if current_user().user_type!="runner": return jsonify({"error":"Runners only"}),403
    data=request.json
    ae=ActiveErrand.query.filter_by(errand_id=data.get("errand_id"),runner_id=current_user().id).first()
    if not ae: return jsonify({"error":"Not found"}),404
    ae.estimated_duration=data.get("duration"); db.session.commit()
    return jsonify({"success":True})

@app.route("/confirm_errand_start/<int:errand_id>", methods=["POST"])
@login_required
def confirm_errand_start(errand_id):
    user=current_user(); errand=Errand.query.get_or_404(errand_id)
    if user.id==errand.client_id:
        rid=request.form.get("runner_id")
        if not rid: flash("Runner not specified","danger"); return redirect(url_for("errandfinal",errand_id=errand.id))
        if not ActiveErrand.query.filter_by(errand_id=errand.id).first(): db.session.add(ActiveErrand(errand_id=errand.id,runner_id=rid,start_time=datetime.utcnow(),status="ongoing"))
        if not Chat.query.filter_by(errand_id=errand.id).first(): db.session.add(Chat(errand_id=errand.id,client_id=user.id,runner_id=rid))
        errand.status="accepted"; errand.agreed_price=float(request.form.get("agreed_price",errand.price_estimate))
        db.session.commit()
        return redirect(url_for("chat_detail",chat_id=Chat.query.filter_by(errand_id=errand.id).first().id))
    return redirect(url_for("home_page"))

@app.route("/negotiate", methods=["POST"])
@login_required
def negotiate():
    errand_id = request.form.get("errand_id")
    runner_id = request.form.get("runner_id")
    if not errand_id or not runner_id:
        return jsonify({"error": "Missing data"}), 400

    existing = Negotiation.query.filter_by(errand_id=errand_id, runner_id=runner_id).first()
    if not existing:
        neg = Negotiation(
            errand_id=errand_id,
            runner_id=runner_id,
            offer_price=0,
            status="pending"
        )
        db.session.add(neg)
        db.session.commit()
    return jsonify({"message": "Offer sent"})

@app.route("/api/check_negotiation", methods=["GET"])
@login_required
def api_check_negotiation():
    errand_id = request.args.get("errand_id")
    runner_id = request.args.get("runner_id")

    if not errand_id or not runner_id:
        return jsonify({"error": "Missing parameters"}), 400

    neg = Negotiation.query.filter_by(
        errand_id=errand_id,
        runner_id=runner_id
    ).first()

    if neg and neg.offer_price and neg.offer_price > 0:
        return jsonify({
            "runner_price": neg.offer_price,
            "status": neg.status
        })
    return jsonify({"runner_price": None})

@app.route("/api/accept_negotiation", methods=["POST"])
@login_required
def accept_negotiation():
    data = request.get_json()
    errand_id = data.get("errand_id")
    runner_id = data.get("runner_id")

    if not errand_id or not runner_id:
        return jsonify({"error": "Missing data"}), 400

    neg = Negotiation.query.filter_by(errand_id=errand_id, runner_id=runner_id).first()
    if neg and neg.offer_price and neg.offer_price > 0:
        neg.status = "accepted"
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "No valid offer found"}), 400


@app.route("/api/cancel_acceptance", methods=["POST"])
@login_required
def cancel_acceptance():
    data = request.get_json()
    errand_id = data.get("errand_id")
    runner_id = data.get("runner_id")
    if not errand_id or not runner_id:
        return jsonify({"error": "Missing data"}), 400

    neg = Negotiation.query.filter_by(errand_id=errand_id, runner_id=runner_id).first()
    if neg:
        if neg.status == "accepted":
            # Revert to pending so runner sees it again
            neg.status = "pending"
            db.session.commit()
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Not in accepted state"}), 400
    return jsonify({"error": "No negotiation found"}), 404

@app.route("/api/delete_negotiation", methods=["POST"])
@login_required
def delete_negotiation():
    data = request.get_json()
    errand_id = data.get("errand_id")
    runner_id = data.get("runner_id")
    if not errand_id or not runner_id:
        return jsonify({"error": "Missing data"}), 400

    neg = Negotiation.query.filter_by(errand_id=errand_id, runner_id=runner_id).first()
    if neg:
        if neg.status == "accepted":
            return jsonify({"error": "Cannot cancel an accepted offer"}), 400
        db.session.delete(neg)
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "No negotiation found"}), 404

@app.route("/api/errand_bids/<int:errand_id>")
@login_required
def errand_bids(errand_id):
    """Return all runner bids for an errand, sorted most recent first."""
    errand = Errand.query.get_or_404(errand_id)
    user = current_user()
    # Only the errand owner (client) or any runner can view bids
    if user.id != errand.client_id and user.user_type != "runner":
        return jsonify({"error": "Unauthorized"}), 403

    # Fetch negotiations ordered by most recent first
    # Use updated_at if available, else id (which increases with time)
    if hasattr(Negotiation, 'updated_at'):
        negs = Negotiation.query.filter_by(errand_id=errand_id)\
            .order_by(Negotiation.updated_at.desc()).all()
    else:
        negs = Negotiation.query.filter_by(errand_id=errand_id)\
            .order_by(Negotiation.id.desc()).all()

    bids = []
    for neg in negs:
        bids.append({
            "runner_id": neg.runner_id,
            "offer_price": neg.offer_price,
            "status": neg.status,
            "updated_at": neg.updated_at.isoformat() if hasattr(neg, 'updated_at') and neg.updated_at else None
        })
    return jsonify({"bids": bids})

# ============================================================================
# BACKGROUND CLEANUP THREAD
# ============================================================================
def cleanup_expired_errands():
    """Deletes errands that are older than 7 minutes and not accepted."""
    while True:
        try:
            with app.app_context():
                now = datetime.utcnow()
                cutoff = now - timedelta(minutes=7)

                deleted = Errand.query.filter(
                    Errand.status.in_(['available', 'pending']),
                    db.or_(
                        Errand.hard_deadline < now,
                        db.and_(Errand.hard_deadline == None, Errand.created_at < cutoff)
                    )
                ).delete(synchronize_session='fetch')

                if deleted > 0:
                    db.session.commit()
                    print(f"🧹 Cleaned up {deleted} expired errand(s) at {now.strftime('%H:%M:%S')}")
                else:
                    db.session.rollback()
        except Exception as e:
            print(f"Cleanup error: {e}")
            try:
                db.session.rollback()
            except:
                pass

        time.sleep(60)

@app.route("/api/runner_offer", methods=["POST"])
@login_required
def runner_offer():
    user = current_user()
    data = request.get_json()
    errand_id = data.get("errand_id")
    offer_price = data.get("offer_price")

    if not errand_id or not offer_price:
        return jsonify({"error": "Missing data"}), 400

    neg = Negotiation.query.filter_by(errand_id=errand_id, runner_id=user.id).first()

    if neg:
        neg.offer_price = offer_price
        neg.status = "pending"
    else:
        neg = Negotiation(
            errand_id=errand_id,
            runner_id=user.id,
            offer_price=offer_price,
            status="pending"
        )
        db.session.add(neg)

    db.session.commit()
    return jsonify({"success": True})

@app.route("/go_to_chat/<int:errand_id>")
@login_required
def go_to_chat(errand_id):
    """Create (or find) the chat for this errand and redirect to it."""
    user = current_user()
    errand = Errand.query.get_or_404(errand_id)

    # Determine the two participants
    if user.id == errand.client_id:
        neg = Negotiation.query.filter_by(errand_id=errand_id, status="accepted").first()
        if not neg:
            flash("No accepted offer for this errand.", "warning")
            return redirect(url_for("home_page"))
        runner_id = neg.runner_id
        client_id = errand.client_id
    else:
        runner_id = user.id
        client_id = errand.client_id

    # Look for an existing chat or create one
    chat = Chat.query.filter_by(errand_id=errand_id, client_id=client_id, runner_id=runner_id).first()
    if not chat:
        chat = Chat(errand_id=errand_id, client_id=client_id, runner_id=runner_id)
        db.session.add(chat)
        db.session.commit()

    return redirect(url_for("chat_detail", chat_id=chat.id))

@app.route("/api/check_client_acceptance", methods=["GET"])
@login_required
def check_client_acceptance():
    user = current_user()
    errand_id = request.args.get("errand_id")
    runner_id = user.id

    if not errand_id:
        return jsonify({"error": "errand_id required"}), 400

    neg = Negotiation.query.filter_by(errand_id=errand_id, runner_id=runner_id).first()
    if neg and neg.status == "accepted":
        return jsonify({
            "accepted": True,
            "agreed_price": neg.offer_price
        })
    else:
        return jsonify({"accepted": False})


@app.route("/api/confirm_proceed", methods=["POST"])
@login_required
def confirm_proceed():
    data = request.get_json()
    errand_id = data.get("errand_id")
    if not errand_id:
        return jsonify({"error": "Missing errand_id"}), 400

    user = current_user()
    errand = Errand.query.get(errand_id)
    if not errand:
        return jsonify({"error": "Errand not found"}), 404

    # Find the accepted negotiation for this errand involving the user
    if user.id == errand.client_id:
        neg = Negotiation.query.filter_by(
            errand_id=errand_id, status="accepted"
        ).first()
        if not neg:
            neg = Negotiation.query.filter_by(
                errand_id=errand_id, status="runner_proceeded"
            ).first()
    else:
        neg = Negotiation.query.filter_by(
            errand_id=errand_id, runner_id=user.id, status="accepted"
        ).first()
        if not neg:
            neg = Negotiation.query.filter_by(
                errand_id=errand_id, runner_id=user.id, status="client_proceeded"
            ).first()

    if not neg:
        return jsonify({"error": "No accepted negotiation found"}), 400

    # Update status based on who is proceeding
    if user.id == errand.client_id:
        if neg.status == "accepted":
            neg.status = "client_proceeded"
        elif neg.status == "runner_proceeded":
            neg.status = "active"
    else:
        if neg.status == "accepted":
            neg.status = "runner_proceeded"
        elif neg.status == "client_proceeded":
            neg.status = "active"

    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/check_proceed", methods=["GET"])
@login_required
def check_proceed():
    errand_id = request.args.get("errand_id")
    if not errand_id:
        return jsonify({"error": "Missing errand_id"}), 400

    user = current_user()
    errand = Errand.query.get(errand_id)
    if not errand:
        return jsonify({"error": "Errand not found"}), 404

    neg = Negotiation.query.filter_by(errand_id=errand_id).filter(
        Negotiation.status.in_(["active", "client_proceeded", "runner_proceeded", "accepted"])
    ).first()

    if not neg:
        return jsonify({"both_proceeded": False, "status": "not_accepted"})

    if neg.status == "active":
        # Both have proceeded – find or create the chat
        chat = Chat.query.filter_by(
            errand_id=errand_id,
            client_id=errand.client_id,
            runner_id=neg.runner_id
        ).first()
        if not chat:
            chat = Chat(
                errand_id=errand_id,
                client_id=errand.client_id,
                runner_id=neg.runner_id
            )
            db.session.add(chat)
            db.session.commit()
        return jsonify({"both_proceeded": True, "chat_id": chat.id})
    else:
        return jsonify({
            "both_proceeded": False,
            "status": neg.status,
            "my_side": "client" if user.id == errand.client_id else "runner"
        })

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
        # id_number = request.form.get("id_number", "").strip()  # REMOVED – column doesn't exist
        country = request.form.get("country", "").strip()
        city = request.form.get("city", "").strip()

        # Validate all required fields
        if not all([first_name, last_name, email, phone_number, date_of_birth, username, password, confirm_password]):
            flash("Please fill all required fields", "danger")
            return redirect(url_for("signup"))

        if user_type == "client" and not terms_agreed:
            flash("You must agree to the Terms of Service and Privacy Policy", "danger")
            return redirect(url_for("signup"))

        if not validate_email(email):
            flash("Please enter a valid email address", "danger")
            return redirect(url_for("signup"))

        if not validate_phone(phone_number):
            flash("Please enter a valid Zimbabwe mobile number (9 digits starting with 71,73,77,78)", "danger")
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

        # REMOVED: duplicate ID number check (id_number column missing)
        # if id_number and User.query.filter_by(id_number=id_number).first():
        #     flash("ID number already registered", "danger")
        #     return redirect(url_for("signup"))

        # Create new user – only include columns that exist in your User model
        full_phone = f"{country_code}{phone_number}"
        fullname = f"{first_name} {last_name}"

        user = User(
            fullname=fullname,
            email=email,
            phone=full_phone,
            username=username,
            password_hash=generate_password_hash(password),
            user_type=user_type
        )
        # Optional fields – comment out if your model doesn't have them
        # user.first_name = first_name
        # user.last_name = last_name
        # user.date_of_birth = date_of_birth
        # user.country = country
        # user.city = city

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
                "phone": full_phone,
                "date_of_birth": date_of_birth,
                "username": username,
                "user_type": user_type
            }
            flash("Registration successful! Please complete your runner profile.", "success")
            return redirect(url_for("runner_signup"))
        else:
            session["user_id"] = user.id
            session["user_type"] = user.user_type
            flash("Registration successful! Welcome to ErrandGo.", "success")
            return redirect(url_for("home_page"))

    return render_template("signup.html")


@app.route('/signup/customer')
def signup_customer():
    return render_template('signup.html', user_type='client')

@app.route('/runner_register', methods=['GET'])
def runner_register():
    return render_template('runner_register.html')

@app.route('/runner_register', methods=['POST'])
def runner_register_post():
    from datetime import datetime

    # Collect data
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    date_of_birth = request.form.get('date_of_birth', '').strip()
    country = request.form.get('country', '').strip()
    city = request.form.get('city', '').strip()
    phone_number = request.form.get('phone_number', '').strip()
    country_code = request.form.get('country_code', '+263').strip()
    email = request.form.get('email', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    errors = []

    # Required fields
    if not all([first_name, last_name, date_of_birth, country, city, phone_number, email, username, password, confirm_password]):
        errors.append("All fields are required.")

    # Email validation
    if not validate_email(email):
        errors.append("Please enter a valid email address.")
    # Phone validation
    if not validate_phone(phone_number):
        errors.append("Invalid Zimbabwe mobile number. Must be 9 digits starting with 71,73,77,78.")
    # Password
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    if password != confirm_password:
        errors.append("Passwords do not match.")
    # Check existing user
    existing = User.query.filter((User.username == username) | (User.email == email)).first()
    if existing:
        errors.append("Username or email already exists.")
    # Age validation
    try:
        dob = datetime.strptime(date_of_birth, '%Y-%m-%d')
        age = (datetime.now().date() - dob.date()).days // 365
        if age < 18:
            errors.append("You must be at least 18 years old to become a runner.")
    except ValueError:
        errors.append("Invalid date of birth format.")

    if errors:
        for error in errors:
            flash(error, 'danger')
        return render_template('runner_register.html', form_data=request.form), 400

    # Corrected User creation – only use existing columns
    full_phone = f"{country_code}{phone_number}"
    fullname = f"{first_name} {last_name}".strip()
    new_user = User(
        fullname=fullname,
        email=email,
        phone=full_phone,
        username=username,
        password_hash=generate_password_hash(password),
        user_type='runner'
    )
    db.session.add(new_user)
    db.session.commit()

    session['user_id'] = new_user.id
    session['user_type'] = 'runner'

    flash("Runner account created successfully! Please complete your profile.", "success")
    return redirect(url_for('runner_signup'))


@app.route("/runner_signup", methods=["GET", "POST"])
@login_required
def runner_signup():
    user = current_user()

    if not user or user.user_type != "runner":
        flash("You must be registered as a runner first.", "danger")
        return redirect(url_for("signup"))

    existing_profile = RunnerProfile.query.filter_by(user_id=user.id).first()
    if existing_profile:
        flash("You already have a runner profile.", "info")
        return redirect(url_for("runnerhome"))

    if request.method == "POST":
        # Collect runner profile fields
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()
        id_number = request.form.get("id_number", "").strip()
        license_number = request.form.get("license_number", "").strip()
        vehicle_type = request.form.get("vehicle_type", "").strip()
        preferred_routes = request.form.get("preferred_routes", "").strip()

        if not all([address, city, id_number, vehicle_type]):
            flash("Please fill all required fields.", "danger")
            return redirect(url_for("runner_signup"))

        # Helper to save uploaded files
        def save_file(file_field, prefix):
            if file_field and file_field.filename:
                filename = secure_filename(f"{user.id}_{prefix}_{file_field.filename}")
                file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                file_field.save(file_path)
                return filename
            return None

        # Save uploaded photos
        id_front = save_file(request.files.get("id_front"), "id_front")
        id_back = save_file(request.files.get("id_back"), "id_back")
        license_front = save_file(request.files.get("license_front"), "license_front")
        license_back = save_file(request.files.get("license_back"), "license_back")
        selfie_left = save_file(request.files.get("selfie_left"), "selfie_left")
        selfie_right = save_file(request.files.get("selfie_right"), "selfie_right")
        selfie_straight = save_file(request.files.get("selfie_straight"), "selfie_straight")
        selfie_with_id = save_file(request.files.get("selfie_with_id"), "selfie_with_id")
        vehicle_front = save_file(request.files.get("vehicle_front"), "vehicle_front")
        vehicle_back = save_file(request.files.get("vehicle_back"), "vehicle_back")
        vehicle_left = save_file(request.files.get("vehicle_left"), "vehicle_left")
        vehicle_right = save_file(request.files.get("vehicle_right"), "vehicle_right")
        car_registration = save_file(request.files.get("car_registration"), "car_registration")

        # Basic photo validation
        if not all([id_front, id_back, license_front, license_back]):
            flash("Please upload both sides of your National ID and Driver's License.", "danger")
            return redirect(url_for("runner_signup"))

        if not all([selfie_left, selfie_right, selfie_straight, selfie_with_id]):
            flash("Please take all four required selfies.", "danger")
            return redirect(url_for("runner_signup"))

        if vehicle_type in ["car", "motorcycle"]:
            if not all([vehicle_front, vehicle_back, vehicle_left, vehicle_right, car_registration]):
                flash("Please upload all required vehicle photos and registration.", "danger")
                return redirect(url_for("runner_signup"))

        # Create RunnerProfile
        profile = RunnerProfile(
            user_id=user.id,
            address=address,
            id_number=id_number,
            vehicle_type=vehicle_type,
            city=city,
            preferred_routes=preferred_routes,
            license_photo=license_front,
            id_photo=id_front,
            id_front=id_front,
            id_back=id_back,
            license_front=license_front,
            license_back=license_back,
            selfie_left=selfie_left,
            selfie_right=selfie_right,
            selfie_straight=selfie_straight,
            selfie_with_id=selfie_with_id,
            vehicle_front=vehicle_front,
            vehicle_back=vehicle_back,
            vehicle_left=vehicle_left,
            vehicle_right=vehicle_right,
            car_registration=car_registration
        )
        db.session.add(profile)
        db.session.commit()

        flash("Runner profile completed! You can now accept errands.", "success")
        return redirect(url_for("runnerhome"))

    return render_template("runner_signup.html", user=user)


@app.route('/roles.html')
def roles():
    return render_template('roles.html')

@app.route('/usertype')
def usertype():
    # Render a page where user chooses "Client" or "Runner"
    return render_template('usertype.html')  # or redirect to signup

@app.route("/api/runner/available_count")
@login_required
def api_runner_available_count():
    user = current_user()
    if user.user_type != "runner":
        return jsonify({"count": 0, "error": "Not a runner"}), 403

    rp = RunnerProfile.query.filter_by(user_id=user.id).first()
    runner_city = rp.city if rp else ""
    now_utc = datetime.utcnow()

    if runner_city:
        count = Errand.query.filter(
            Errand.status.in_(['available', 'pending']),
            Errand.expires_at > now_utc,
            Errand.pickup_location.ilike(f"%{runner_city}%")
        ).count()
    else:
        count = Errand.query.filter(
            Errand.status.in_(['available', 'pending']),
            Errand.expires_at > now_utc
        ).count()

    return jsonify({"count": count})

@app.route("/api/update_stage_progress", methods=["POST"])
@login_required
def update_stage_progress():
    data = request.get_json()
    errand_id = data.get("errand_id")
    stages = data.get("stages")
    if not errand_id or stages is None:
        return jsonify({"error": "Missing data"}), 400
    ae = ActiveErrand.query.filter_by(errand_id=errand_id).first()
    if not ae:
        return jsonify({"error": "Not found"}), 404
    ae.stage_progress = json.dumps(stages)
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/get_stage_progress", methods=["GET"])
@login_required
def get_stage_progress():
    errand_id = request.args.get("errand_id")
    if not errand_id:
        return jsonify({"stages": [False]*6, "runner_marked_complete": False})
    ae = ActiveErrand.query.filter_by(errand_id=errand_id).first()
    if not ae:
        return jsonify({"stages": [False]*6, "runner_marked_complete": False})
    try:
        stages = json.loads(ae.stage_progress) if ae.stage_progress else [False]*6
    except:
        stages = [False]*6
    return jsonify({
        "stages": stages, 
        "runner_marked_complete": ae.runner_marked_complete or False
    })

@app.route("/api/runner_mark_complete", methods=["POST"])
@login_required
def runner_mark_complete():
    data = request.get_json()
    errand_id = data.get("errand_id")
    if not errand_id:
        return jsonify({"error": "Missing errand_id"}), 400
    ae = ActiveErrand.query.filter_by(errand_id=errand_id).first()
    if not ae:
        return jsonify({"error": "Not found"}), 404
    ae.runner_marked_complete = True
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/complete_errand", methods=["POST"])
@login_required
def complete_errand():
    data = request.get_json()
    errand_id = data.get("errand_id")
    if not errand_id:
        return jsonify({"error": "Missing errand_id"}), 400
    errand = Errand.query.get(errand_id)
    ae = ActiveErrand.query.filter_by(errand_id=errand_id).first()
    if not errand or not ae:
        return jsonify({"error": "Not found"}), 404
    errand.status = "completed"
    ae.status = "completed"
    ae.end_time = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})

if __name__ == "__main__":
    cleanup_thread = threading.Thread(target=cleanup_expired_errands, daemon=True)
    cleanup_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=True)