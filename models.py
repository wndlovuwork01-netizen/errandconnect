from datetime import datetime
from extensions import db


# ============================================================
# USER MODEL
# ============================================================
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(200), nullable=False)
    user_type = db.Column(db.String(20), nullable=False, default="client")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    runner_profile = db.relationship("RunnerProfile", back_populates="user", uselist=False)
    errands = db.relationship("Errand", back_populates="client", lazy=True)
    negotiations = db.relationship("Negotiation", back_populates="runner", lazy=True)
    active_errands = db.relationship("ActiveErrand", back_populates="runner", lazy=True)
    sent_ratings = db.relationship("Rating", foreign_keys="Rating.from_user_id", backref="rater", lazy=True)
    received_ratings = db.relationship("Rating", foreign_keys="Rating.to_user_id", backref="rated_user", lazy=True)
    notifications = db.relationship("Notification", back_populates="user", lazy=True)

    def __repr__(self):
        return f"<User {self.username}>"

    @property
    def average_rating(self):
        from models import Rating  # Import here to avoid circular import
        ratings = Rating.query.filter_by(to_user_id=self.id).all()
        if not ratings:
            return 0
        return sum(r.rating for r in ratings) / len(ratings)


# ============================================================
# RUNNER PROFILE MODEL
# ============================================================
class RunnerProfile(db.Model):
    __tablename__ = "runner_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    dob = db.Column(db.String(50))
    address = db.Column(db.String(300))
    id_number = db.Column(db.String(100))
    vehicle_type = db.Column(db.String(50))
    city = db.Column(db.String(100))
    preferred_routes = db.Column(db.String(500))
    license_photo = db.Column(db.String(300))
    id_photo = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="runner_profile")

    def __repr__(self):
        return f"<RunnerProfile user_id={self.user_id}>"


# ============================================================
# ERRAND MODEL
# ============================================================
class Errand(db.Model):
    __tablename__ = "errands"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.String(100))
    pickup_location = db.Column(db.String(300))
    delivery_location = db.Column(db.String(300))
    weight = db.Column(db.String(50))
    delivery_time = db.Column(db.String(50))
    details = db.Column(db.Text)
    price_estimate = db.Column(db.Float)
    agreed_price = db.Column(db.Float)  # Final agreed price
    calculated_minimum_fee = db.Column(db.Float)
    status = db.Column(db.String(50), default="pending")  # pending, accepted, completed, cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("User", back_populates="errands")
    negotiations = db.relationship("Negotiation", back_populates="errand", lazy=True)
    active_errand = db.relationship("ActiveErrand", back_populates="errand", uselist=False)
    ratings = db.relationship("Rating", backref="errand_rated", lazy=True)
    chats = db.relationship("Chat", back_populates="errand", lazy=True)

    def __repr__(self):
        return f"<Errand id={self.id} client_id={self.client_id}>"


# ============================================================
# NEGOTIATION MODEL
# ============================================================
class Negotiation(db.Model):
    __tablename__ = "negotiations"

    id = db.Column(db.Integer, primary_key=True)
    errand_id = db.Column(db.Integer, db.ForeignKey("errands.id"), nullable=False)
    runner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    offer_price = db.Column(db.Float)
    status = db.Column(db.String(50), default="pending")  # pending, accepted, rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    errand = db.relationship("Errand", back_populates="negotiations")
    runner = db.relationship("User", back_populates="negotiations")

    def __repr__(self):
        return f"<Negotiation errand_id={self.errand_id} runner_id={self.runner_id}>"


# ============================================================
# CHAT MODEL
# ============================================================
class Chat(db.Model):
    __tablename__ = "chats"

    id = db.Column(db.Integer, primary_key=True)
    errand_id = db.Column(db.Integer, db.ForeignKey("errands.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    runner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    errand = db.relationship("Errand", back_populates="chats")
    client = db.relationship("User", foreign_keys=[client_id], backref="client_chats")
    runner = db.relationship("User", foreign_keys=[runner_id], backref="runner_chats")
    messages = db.relationship("Message", back_populates="chat", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Chat errand={self.errand_id}>"


# ============================================================
# MESSAGE MODEL
# ============================================================
class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chats.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chat = db.relationship("Chat", back_populates="messages")
    sender = db.relationship("User", foreign_keys=[sender_id])

    def __repr__(self):
        return f"<Message {self.id} from {self.sender_id}>"


# ============================================================
# ACTIVE ERRAND MODEL
# ============================================================
class ActiveErrand(db.Model):
    __tablename__ = "active_errands"

    id = db.Column(db.Integer, primary_key=True)
    errand_id = db.Column(db.Integer, db.ForeignKey("errands.id"), nullable=False)
    runner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime, nullable=True)
    estimated_duration = db.Column(db.String(100)) # e.g. "15 mins"
    status = db.Column(db.String(50))  # ongoing, completed, cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    errand = db.relationship("Errand", back_populates="active_errand")
    runner = db.relationship("User", back_populates="active_errands")

    def __repr__(self):
        return f"<ActiveErrand errand_id={self.errand_id} runner_id={self.runner_id}>"


# ============================================================
# RATING MODEL
# ============================================================
class Rating(db.Model):
    __tablename__ = "ratings"

    id = db.Column(db.Integer, primary_key=True)
    errand_id = db.Column(db.Integer, db.ForeignKey("errands.id"), nullable=False)
    from_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)  # 1-5 stars
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Rating {self.rating} stars for user {self.to_user_id}>"


# ============================================================
# NOTIFICATION MODEL
# ============================================================
class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="notifications")

    def __repr__(self):
        return f"<Notification for user {self.user_id}>"


# ============================================================
# APP FEEDBACK MODEL - NEWLY ADDED
# ============================================================
class AppFeedback(db.Model):
    __tablename__ = "app_feedbacks"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)  # 1-5 stars
    feedback_type = db.Column(db.String(50))  # general, bug, suggestion, service, app
    feedback = db.Column(db.Text, nullable=False)
    suggestions = db.Column(db.Text)
    contact_permission = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("app_feedbacks", lazy=True))

    def __repr__(self):
        return f"<AppFeedback from user {self.user_id}: {self.rating} stars>"


# ============================================================
# FEE CONFIGURATION MODEL
# ============================================================
class FeeConfig(db.Model):
    __tablename__ = "fee_configs"

    id = db.Column(db.Integer, primary_key=True)
    base_fee = db.Column(db.Float, nullable=False)
    per_km_fee = db.Column(db.Float, nullable=False)
    per_kg_fee = db.Column(db.Float, nullable=False)
    night_multiplier = db.Column(db.Float, nullable=False)
    rush_hour_multiplier = db.Column(db.Float, nullable=False)
    vehicle_type_multiplier_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FeeConfig id={self.id}>"