from datetime import datetime, date
from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime, Enum, ForeignKey, Boolean, Text, Index, Enum as SAEnum, CheckConstraint, func, UniqueConstraint, Table
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from config import Config
from utils import EncryptedBytes
from enum import Enum as PyEnum
import enum
from werkzeug.security import generate_password_hash, check_password_hash

engine = create_engine(Config.SQLALCHEMY_DATABASE_URI, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

contact_projects = Table(
    "contact_projects",
    Base.metadata,
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    UniqueConstraint("contact_id", "project_id", name="uq_contact_project")
)

class RequestStatus(str, PyEnum):
    PENDING   = "Pending"
    COMPLETED = "Completed"

class ProjectStatus(str, PyEnum):
    PLANNED   = "Planned"
    ACTIVE    = "Active"
    COMPLETED = "Completed"

class FoiaRequest(Base):
    __tablename__ = 'foia_requests'
    id = Column(Integer, primary_key=True)
    reference_number = Column(String(64), unique=True, index=True)
    agency = Column(String(255))
    request_date = Column(Date)
    completed_date = Column(Date, nullable=True)
    status = Column(SAEnum(RequestStatus), nullable=False, default=RequestStatus.PENDING)
    last_reminder_at = Column(Date, nullable=True)

    subject = Column(EncryptedBytes)
    snippet = Column(EncryptedBytes)

    thread_id = Column(String(128))
    first_message_id = Column(String(128))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    events = relationship("FoiaEvent", back_populates="request", cascade="all, delete-orphan")
    attachments = relationship("FoiaAttachment", back_populates="request", cascade="all, delete-orphan")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    project = relationship("Project", backref="foia_requests")
    followups = relationship(
        "FoiaFollowUp",
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="desc(FoiaFollowUp.fu_date), desc(FoiaFollowUp.id)"
    )

class FoiaEvent(Base):
    __tablename__ = 'foia_events'
    id = Column(Integer, primary_key=True)
    foia_request_id = Column(Integer, ForeignKey('foia_requests.id', ondelete="CASCADE"))
    event_type = Column(String(32))  # ack | response | note
    timestamp = Column(DateTime, default=datetime.utcnow)
    message_id = Column(String(128))
    body = Column(EncryptedBytes)

    request = relationship("FoiaRequest", back_populates="events")

class FoiaAttachment(Base):
    __tablename__ = 'foia_attachments'
    id = Column(Integer, primary_key=True)
    foia_request_id = Column(Integer, ForeignKey('foia_requests.id', ondelete="CASCADE"))
    filename = Column(String(512))
    mime_type = Column(String(128))
    size = Column(Integer)

    # Encrypted on disk; we store path to .enc
    stored_path = Column(String(1024))
    ocr_pdf_path = Column(String(1024), nullable=True)  # decrypted OCR’d copy (temp/cache)

    is_encrypted = Column(Boolean, default=True)

    request = relationship("FoiaRequest", back_populates="attachments")

class CourtCase(Base):
    __tablename__ = 'court_cases'
    id = Column(Integer, primary_key=True)
    cause_number = Column(String(64), unique=True, index=True)
    defendant_name = Column(String(255))
    file_date = Column(Date, nullable=True)
    charges = Column(Text)  # full text
    disposition = Column(String(64))  # e.g., Dismissed, Convicted, Pending
    conviction_type = Column(String(32))  # Jury, Bench, Plea, N/A
    conviction_date = Column(Date, nullable=True)
    sentence_total_months = Column(Integer, nullable=True)
    sentence_executed_months = Column(Integer, nullable=True)
    sentence_suspended_months = Column(Integer, nullable=True)
    max_sentence_months = Column(Integer, nullable=True)

class SurroundingCase(Base):
    __tablename__ = 'surrounding_cases'
    id = Column(Integer, primary_key=True)
    cause_number = Column(String(64), unique=True, index=True)
    defendant_name = Column(String(255))
    file_date = Column(Date, nullable=True)
    charges = Column(Text)  # full text
    disposition = Column(String(64))  # e.g., Dismissed, Convicted, Pending
    conviction_type = Column(String(32))  # Jury, Bench, Plea, N/A
    conviction_date = Column(Date, nullable=True)
    sentence_total_months = Column(Integer, nullable=True)
    sentence_executed_months = Column(Integer, nullable=True)
    sentence_suspended_months = Column(Integer, nullable=True)
    max_sentence_months = Column(Integer, nullable=True)

class ProjectDocument(Base):
    __tablename__ = "project_documents"

    id = Column(Integer, primary_key=True)
    project_slug = Column(String, index=True)   # e.g., "mcpo-plea-deals"
    title = Column(String, nullable=False)      # display name (editable)
    filename = Column(String, nullable=False)   # stored filename on disk
    stored_path = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    size = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)         # editable notes
    uploaded_at = Column(DateTime, default=datetime.utcnow)

class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    slug = Column(String(100), unique=True, nullable=False, index=True)  # e.g., "mcpo-plea-deals"
    name = Column(String(200), nullable=False)
    status = Column(SAEnum(ProjectStatus), nullable=False, default=ProjectStatus.PLANNED)
    created_at = Column(DateTime, default=datetime.utcnow)
    deadline = Column(Date, nullable=True)                 # NEW
    last_deadline_alert = Column(Date, nullable=True)      # NEW

    notes = relationship("ProjectNote", back_populates="project", cascade="all, delete-orphan")

    contacts = relationship(
        "Contact",
        secondary=contact_projects,
        back_populates="projects",
        lazy="selectin",
    )

class ProjectNote(Base):
    __tablename__ = "project_notes"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    project = relationship("Project", back_populates="notes")

class WorkbenchDataset(Base):
    __tablename__ = "workbench_datasets"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    stored_path = Column(String(1024), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    # Optional: which column holds the defendant names
    defendant_col = Column(String(128), default="defendant_name")

    records = relationship("WorkbenchRecordLink", back_populates="dataset", cascade="all, delete-orphan")
    links = relationship("WorkbenchRecordLink", back_populates="dataset", cascade="all, delete-orphan")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    project = relationship("Project", backref="workbench_datasets")

class WorkbenchRecordLink(Base):
    __tablename__ = "workbench_record_links"
    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("workbench_datasets.id", ondelete="CASCADE"))
    row_index = Column(Integer, nullable=False)
    raw_defendant = Column(String(512), nullable=True)
    matched_case_id = Column(Integer, ForeignKey("court_cases.id"), nullable=True)  # if you have CourtCase.defendant_name
    match_type = Column(String(32), nullable=True)  # "exact", "normalized", etc.

    dataset = relationship("WorkbenchDataset", back_populates="records")
    matched_case = relationship("CourtCase", lazy="joined", viewonly=True)

Index("idx_project_notes_recent", ProjectNote.project_id, ProjectNote.created_at.desc())

class WorkbenchPdfLink(Base):
    __tablename__ = "workbench_pdf_links"
    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("workbench_datasets.id"), index=True, nullable=False)
    doc_id = Column(Integer, ForeignKey("project_documents.id"), index=True, nullable=False)
    key_value = Column(String, nullable=False)   # the grouped value (e.g., a defendant name)
    score = Column(Integer, default=0)           # simple occurrence count
    created_at = Column(DateTime, default=datetime.utcnow)

    dataset = relationship("WorkbenchDataset", backref="pdf_links")
    document = relationship("ProjectDocument")

class Entity(Base):
    __tablename__ = "entities"
    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False, index=True)
    # 'person' or 'org'
    kind = Column(String(16), nullable=False, default="person")
    __table_args__ = (CheckConstraint("kind in ('person','org')"),)

class EntityMention(Base):
    __tablename__ = "entity_mentions"
    id = Column(Integer, primary_key=True)
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    # IMPORTANT: point to project_documents
    doc_id = Column(Integer, ForeignKey("project_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    # Flask-Login integration helpers:
    @property
    def is_authenticated(self): return True
    @property
    def is_active(self): return True
    @property
    def is_anonymous(self): return False
    def get_id(self): return str(self.id)

    # password helpers
    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)
    
class MediaItem(Base):
    __tablename__ = "media_items"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    title = Column(Text)
    filename = Column(Text)
    stored_path = Column(Text, nullable=False)
    mime_type = Column(Text)
    duration_seconds = Column(Integer)
    notes = Column(Text)
    transcript_text = Column(Text)      # full transcript (for search/export)
    transcript_json = Column(Text)      # JSON-serialized segments [{start,end,text}]
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", backref="media_items")

# optional tiny FTS for media (separate from doc_fts)
def ensure_av_fts(engine):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS av_fts USING fts5(
              media_id UNINDEXED,
              title,
              body,
              tokenize='porter'
            );
        """))

class CaseNotebookEntry(Base):
    __tablename__ = "case_notebook_entries"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    kind = Column(Text, nullable=False)  # 'fact','lead','question','quote','task','source'
    title = Column(Text, nullable=False)
    body = Column(Text)
    source_url = Column(Text)
    source_doc_id = Column(Integer, nullable=True)   # optional link to ProjectDocument.id
    source_page = Column(Integer, nullable=True)
    is_pinned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", backref="notebook_entries")

class FoiaFollowUp(Base):
    __tablename__ = "foia_followups"

    id = Column(Integer, primary_key=True)
    foia_request_id = Column(Integer, ForeignKey("foia_requests.id"), nullable=False, index=True)

    # When you followed up (email/phone/in-person)
    fu_date = Column(Date, nullable=True)

    # Keep this a plain String for portability; we validate in the view.
    # Allowed: "E-Mail", "Phone", "In-Person"
    method = Column(String(20), nullable=False, default="E-Mail")

    # Reply tracking
    reply_received = Column(Boolean, nullable=False, default=False)
    reply_date = Column(Date, nullable=True)

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship back to FoiaRequest
    request = relationship("FoiaRequest", back_populates="followups")

class Tip(Base):
    __tablename__ = "tips"
    id = Column(Integer, primary_key=True)
    glk_id = Column(String, unique=True, index=True, nullable=False)   # Globaleaks tip id
    status = Column(String(40))                                        # e.g., 'new','open','closed'
    title = Column(String(255))
    summary = Column(Text)                                             # short text / first lines
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)

    project = relationship("Project", lazy="joined")

class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    start_dt = Column(DateTime, nullable=False)          # interpret as LOCAL time
    end_dt   = Column(DateTime, nullable=True)           # optional; else default 30min
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    foia_request_id = Column(Integer, ForeignKey("foia_requests.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", lazy="joined", viewonly=True)
    request = relationship("FoiaRequest", lazy="joined", viewonly=True)

class ContactType(str, PyEnum):
    SOURCE  = "Source"
    CONTACT = "Contact"

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)

    first_name = Column(String(120), nullable=True)
    last_name  = Column(String(120), nullable=True)
    entity     = Column(String(255), nullable=True)
    email      = Column(String(255), nullable=True, index=True)
    phone      = Column(String(64),  nullable=True)

    kind = Column(SAEnum(ContactType), nullable=True)  # dropdown: Source/Contact (optional)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # NEW: many-to-many
    projects = relationship(
        "Project",
        secondary=contact_projects,
        back_populates="contacts",
        lazy="joined",
    )

    @property
    def display_name(self) -> str:
        parts = [self.first_name or "", self.last_name or ""]
        base = " ".join(p for p in parts if p).strip() or (self.entity or "(Unnamed)")
        if self.entity and self.entity.strip() and base.lower() != self.entity.strip().lower():
            base = f"{base} — {self.entity}"
        return base

def init_db():
    Base.metadata.create_all(engine)