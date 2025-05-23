# app/models.py (assuming your new structure is app/models.py)
from sqlalchemy import Column, String, Text, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime
import uuid

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), unique=True, nullable=False)
    keycloak_id = Column(String, unique=True, index=True, nullable=False) # The 'sub' from Keycloak
    username = Column(String, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)

    topics = relationship("Topic", back_populates="owner", cascade="all, delete-orphan") # Cascade deletion of topics

    def __repr__(self):
        return f"<User(username='{self.username}', keycloak_id='{self.keycloak_id}')>"

class Topic(Base):
    __tablename__ = "topics"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), unique=True, nullable=False)
    name = Column(String, index=True, nullable=False)
    is_private = Column(Boolean, default=True, nullable=False) # True for private, False for public
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    owner_id = Column(String, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="topics")

    notes = relationship("Note", back_populates="topic", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="topic", cascade="all, delete-orphan")
    video_links = relationship("VideoLink", back_populates="topic", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Topic(name='{self.name}', owner_id='{self.owner_id}', is_private={self.is_private})>"

class Note(Base):
    __tablename__ = "notes"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), unique=True, nullable=False)
    title = Column(String, index=True, nullable=False)
    content = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    topic_id = Column(String, ForeignKey("topics.id"), nullable=False)
    topic = relationship("Topic", back_populates="notes")

    def __repr__(self):
        return f"<Note(title='{self.title}', topic_id='{self.topic_id}')>"

class Document(Base):
    __tablename__ = "documents"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), unique=True, nullable=False)
    filename = Column(String, nullable=False)
    s3_key = Column(String, unique=True, nullable=False) # Key in MinIO
    file_size_bytes = Column(String, nullable=True) # Store as string to avoid int limits
    content_type = Column(String, nullable=True) # e.g., 'application/pdf', 'image/png'
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    topic_id = Column(String, ForeignKey("topics.id"), nullable=False)
    topic = relationship("Topic", back_populates="documents")

    def __repr__(self):
        return f"<Document(filename='{self.filename}', s3_key='{self.s3_key}')>"

class VideoLink(Base):
    __tablename__ = "video_links"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), unique=True, nullable=False)
    title = Column(String, nullable=False)
    url = Column(String, nullable=False) # The YouTube or video URL
    description = Column(Text, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    topic_id = Column(String, ForeignKey("topics.id"), nullable=False)
    topic = relationship("Topic", back_populates="video_links")

    def __repr__(self):
        return f"<VideoLink(title='{self.title}', url='{self.url}')>"