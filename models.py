from datetime import datetime, date
from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime, Enum, ForeignKey, Boolean, Text, Index, Enum as SAEnum
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from config import Config
from utils import EncryptedBytes
from enum import Enum as PyEnum
import enum

engine = create_engine(Config.SQLALCHEMY_DATABASE_URI, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

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

def init_db():
    Base.metadata.create_all(engine)