# app/main.py (assuming your new structure is app/main.py)
import os
import uuid
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from starlette.config import Config
import boto3 # For MinIO integration

# Import SQLAlchemy components and models (note: now absolute imports from 'app')
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload # Add this import
from sqlalchemy import select, delete
from app.database import get_db, create_tables
from app.models import User, Topic, Note, Document, VideoLink, Base
from pydantic import BaseModel, HttpUrl # HttpUrl for URL validation

# --- MinIO/S3 Configuration ---
S3_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
BUCKET_NAME = os.getenv("MINIO_BUCKET_NAME", "mybucket") # Use a different bucket name if you want to avoid collision

s3_client = boto3.client( # Renamed 's3' to 's3_client' to avoid variable name conflict
    's3',
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    region_name='us-east-1' # MinIO doesn't strictly use regions, but boto3 requires it
)

# --- FastAPI App Setup ---
config = Config('.env')
oauth = OAuth(config)

KEYCLOAK_ISSUER = config('KEYCLOAK_ISSUER', default='http://localhost:8080/realms/myrealm')
KEYCLOAK_CLIENT_ID = config('KEYCLOAK_CLIENT_ID', default='accountability_partner')
KEYCLOAK_CLIENT_SECRET = config('KEYCLOAK_CLIENT_SECRET', default='your_client_secret')

oauth.register(
    name='keycloak',
    client_id=KEYCLOAK_CLIENT_ID,
    client_secret=KEYCLOAK_CLIENT_SECRET,
    server_metadata_url=f'{KEYCLOAK_ISSUER}/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid profile email'},
)

app = FastAPI()
load_dotenv() # Load environment variables from .env
app.add_middleware(SessionMiddleware, secret_key=os.urandom(32))

# --- Dependency to get current user's DB ID ---
async def get_current_user_id(request: Request):
    user_id = request.session.get('user_id')
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# --- Dependency to get current User ORM object ---
async def get_current_user_obj(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get('user_id')
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found in database")
    return user

# --- Startup Event: Create Tables and Ensure MinIO Bucket ---
@app.on_event("startup")
async def startup_event():
    await create_tables()
    print("Database tables ensured (SQLite).")
    try:
        s3_client.head_bucket(Bucket=BUCKET_NAME)
        print(f"MinIO bucket '{BUCKET_NAME}' already exists.")
    except s3_client.exceptions.ClientError as e:
        error_code = int(e.response['Error']['Code'])
        if error_code == 404:
            s3_client.create_bucket(Bucket=BUCKET_NAME)
            print(f"MinIO bucket '{BUCKET_NAME}' created.")
        else:
            raise

# --- Pydantic Models for Request Bodies ---
class TopicCreate(BaseModel):
    name: str
    is_private: bool = True # Default to private

class NoteCreate(BaseModel):
    title: str
    content: Optional[str] = None

class VideoLinkCreate(BaseModel):
    title: str
    url: HttpUrl # Pydantic will validate this as a URL
    description: Optional[str] = None

# --- FastAPI Routes ---

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    user_id = request.session.get('user_id')
    if user_id:
        return f"""
        <h1>Welcome, {request.session.get('user_name')}!</h1>
        <p><a href="/topics">Go to your Topics</a></p>
        <p><a href="/auth/logout">Logout</a></p>
        """
    return """
    <h1>Welcome to Notes App!</h1>
    <p><a href="/auth/login">Login with Keycloak</a></p>
    """

@app.get("/auth/login")
async def login(request: Request):
    redirect_uri = request.url_for('auth_callback')
    print(f"Redirecting to: {redirect_uri}")
    return await oauth.keycloak.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        token = await oauth.keycloak.authorize_access_token(request)
        userinfo = token.get('userinfo')

        if not userinfo:
            raise ValueError("User info (parsed ID Token claims) not found.")

        keycloak_id = userinfo.get('sub')
        username = userinfo.get('preferred_username', userinfo.get('name', 'unknown'))
        email = userinfo.get('email', 'no-email@example.com')

        result = await db.execute(select(User).where(User.keycloak_id == keycloak_id))
        user = result.scalar_one_or_none()

        if not user:
            user = User(keycloak_id=keycloak_id, username=username, email=email)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            print(f"New user created in DB: {user.username} (ID: {user.id})")
        else:
            if user.username != username or user.email != email:
                user.username = username
                user.email = email
                await db.commit()
                await db.refresh(user)
            print(f"Existing user logged in: {user.username} (ID: {user.id})")

        request.session['user_id'] = user.id
        request.session['user_name'] = user.username
        request.session['keycloak_sub'] = keycloak_id

        return RedirectResponse(url="/topics") # Redirect to topics page

    except Exception as e:
        print(f"Caught exception in auth_callback: {e}")
        raise HTTPException(status_code=400, detail=f"Authentication failed: {e}")

@app.get("/auth/logout")
async def logout(request: Request):
    if 'user_id' in request.session:
        del request.session['user_id']
    if 'user_name' in request.session:
        del request.session['user_name']
    if 'keycloak_sub' in request.session:
        del request.session['keycloak_sub']
    return RedirectResponse(url="/")

# --- Topic Endpoints ---
@app.get("/topics", response_class=HTMLResponse)
async def list_topics_page(request: Request, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    # Get private topics
    # No need to eager load owner for private topics if you don't display owner info
    private_topics_result = await db.execute(
        select(Topic).where(Topic.owner_id == current_user.id, Topic.is_private == True).order_by(Topic.name)
    )
    private_topics = private_topics_result.scalars().all()

    # Get public topics (owned by user and public, or public topics by other users)
    # FIX: Eager load the 'owner' relationship using selectinload
    public_topics_result = await db.execute(
        select(Topic).options(selectinload(Topic.owner)).where(Topic.is_private == False).order_by(Topic.name)
    )
    public_topics = public_topics_result.scalars().all()

    user_name = request.session.get('user_name', 'User')

    private_html = ""
    if private_topics:
        private_html = "<h2>Your Private Topics:</h2><ul>"
        for topic in private_topics:
            private_html += f"<li><a href='/topics/{topic.id}'>{topic.name}</a> (Private) <a href='/topics/{topic.id}/delete' style='color:red;'>Delete</a></li>"
        private_html += "</ul>"
    else:
        private_html = "<p>No private topics yet. <a href='/topics/new?is_private=true'>Create one!</a></p>"

    public_html = ""
    if public_topics:
        public_html = "<h2>Public Topics:</h2><ul>"
        for topic in public_topics:
            # Now topic.owner should be loaded without a new DB call
            owner_info = f" (by {topic.owner.username})" if topic.owner_id != current_user.id else " (Your Public Topic)"
            delete_link = f" <a href='/topics/{topic.id}/delete' style='color:red;'>Delete</a>" if topic.owner_id == current_user.id else ""
            public_html += f"<li><a href='/topics/{topic.id}'>{topic.name}</a>{owner_info}{delete_link}</li>"
        public_html += "</ul>"
    else:
        public_html = "<p>No public topics available.</p>"


    return f"""
    <h1>Topics for {user_name}</h1>
    {private_html}
    <p><a href="/topics/new?is_private=true">Create New Private Topic</a></p>
    {public_html}
    <p><a href="/topics/new?is_private=false">Create New Public Topic</a></p>
    <p><a href="/auth/logout">Logout</a></p>
    """


@app.get("/topics/new", response_class=HTMLResponse)
async def new_topic_page(request: Request, is_private: bool = True):
    topic_type = "Private" if is_private else "Public"
    return f"""
    <h1>Create New {topic_type} Topic</h1>
    <form id="topicForm">
        <label for="name">Topic Name:</label><br>
        <input type="text" id="name" name="name" required><br><br>
        <input type="hidden" id="is_private" name="is_private" value="{str(is_private).lower()}">
        <button type="submit">Create Topic</button>
    </form>
    <div id="responseMessage"></div>
    <script>
        document.getElementById('topicForm').addEventListener('submit', async function(event) {{
            event.preventDefault();
            const name = document.getElementById('name').value;
            const is_private = document.getElementById('is_private').value === 'true';
            const responseMessage = document.getElementById('responseMessage');

            try {{
                const response = await fetch('/topics', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{ name, is_private }})
                }});

                const data = await response.json();
                if (response.ok) {{
                    responseMessage.textContent = 'Topic created successfully! ID: ' + data.topic_id;
                    responseMessage.style.color = 'green';
                    document.getElementById('name').value = '';
                    window.location.href = '/topics'; // Redirect to topics list
                }} else {{
                    responseMessage.textContent = 'Error: ' + (data.detail || 'Failed to create topic');
                    responseMessage.style.color = 'red';
                }}
            }} catch (error) {{
                responseMessage.textContent = 'Network error: ' + error.message;
                responseMessage.style.color = 'red';
            }}
        }});
    </script>
    <p><a href="/topics">Back to Topics</a></p>
    """

@app.post("/topics", status_code=201)
async def create_topic(topic_data: TopicCreate, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    new_topic = Topic(
        name=topic_data.name,
        is_private=topic_data.is_private,
        owner_id=current_user.id
    )
    db.add(new_topic)
    await db.commit()
    await db.refresh(new_topic)
    return {"message": "Topic created successfully", "topic_id": new_topic.id}

@app.get("/topics/{topic_id}/delete", response_class=HTMLResponse)
async def confirm_delete_topic_page(topic_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()

    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    if topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this topic")

    return f"""
    <h1>Confirm Delete Topic: {topic.name}</h1>
    <p>Are you sure you want to delete this topic and all its contents (notes, documents, videos)? This action cannot be undone.</p>
    <form action="/topics/{topic_id}" method="post">
        <input type="hidden" name="method" value="DELETE">
        <button type="submit" style="color:red;">Yes, Delete Topic</button>
    </form>
    <p><a href="/topics/{topic_id}">Cancel</a> | <a href="/topics">Back to Topics</a></p>
    """

@app.post("/topics/{topic_id}") # Using POST for form submission, will handle DELETE via hidden input
async def handle_delete_topic_form(topic_id: str, method: str = Form(...), current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    if method == "DELETE":
        return await delete_topic(topic_id, current_user, db)
    raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.delete("/topics/{topic_id}", status_code=204) # API endpoint for DELETE
async def delete_topic(topic_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()

    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    if topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this topic")

    # IMPORTANT: Delete associated files from MinIO BEFORE deleting the DB record
    documents_result = await db.execute(select(Document).where(Document.topic_id == topic_id))
    documents = documents_result.scalars().all()
    for doc in documents:
        try:
            s3_client.delete_object(Bucket=BUCKET_NAME, Key=doc.s3_key)
            print(f"Deleted S3 object: {doc.s3_key}")
        except Exception as e:
            print(f"Error deleting S3 object {doc.s3_key}: {e}") # Log error, but continue

    await db.delete(topic)
    await db.commit()
    return Response(status_code=204) # No content for successful deletion

# --- Topic Detail Page (Notes, Documents, Videos) ---
@app.get("/topics/{topic_id}", response_class=HTMLResponse)
async def get_topic_detail_page(topic_id: str, request: Request, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Topic).where(Topic.id == topic_id).options(selectinload(Topic.owner)))
    topic = result.scalar_one_or_none()

    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    # Access control for private topics
    if topic.is_private and topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this private topic")

    # Fetch associated items
    notes_result = await db.execute(select(Note).where(Note.topic_id == topic_id).order_by(Note.created_at.desc()))
    notes = notes_result.scalars().all()

    documents_result = await db.execute(select(Document).where(Document.topic_id == topic_id).order_by(Document.uploaded_at.desc()))
    documents = documents_result.scalars().all()

    video_links_result = await db.execute(select(VideoLink).where(VideoLink.topic_id == topic_id).order_by(VideoLink.added_at.desc()))
    video_links = video_links_result.scalars().all()

    notes_html = "<h3>Notes:</h3><ul>"
    if notes:
        for note in notes:
            display_content = (note.content[:100] + '...') if note.content and len(note.content) > 100 else (note.content if note.content else '')
            notes_html += f"<li><strong>{note.title}</strong>: {display_content} <a href='/topics/{topic.id}/notes/{note.id}'>Read More</a>"
            if topic.owner_id == current_user.id: # Only owner can delete notes
                 notes_html += f" <a href='/topics/{topic.id}/notes/{note.id}/delete' style='color:red;'>Delete</a>"
            notes_html += "</li>"
    else:
        notes_html += "<li>No notes yet.</li>"
    notes_html += "</ul>"

    documents_html = "<h3>Documents:</h3><ul>"
    if documents:
        for doc in documents:
            # Generate pre-signed URL for each document
            download_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': BUCKET_NAME, 'Key': doc.s3_key},
                ExpiresIn=300
            )
            documents_html += f"<li><a href='{download_url}' target='_blank'>{doc.filename}</a> ({round(float(doc.file_size_bytes)/1024/1024, 2) if doc.file_size_bytes else 'N/A'} MB)"
            if topic.owner_id == current_user.id: # Only owner can delete documents
                 documents_html += f" <a href='/topics/{topic.id}/documents/{doc.id}/delete' style='color:red;'>Delete</a>"
            documents_html += "</li>"
    else:
        documents_html += "<li>No documents yet.</li>"
    documents_html += "</ul>"

    video_links_html = "<h3>Video Links:</h3><ul>"
    if video_links:
        for video in video_links:
            video_links_html += f"<li><a href='{video.url}' target='_blank'>{video.title}</a>"
            if topic.owner_id == current_user.id: # Only owner can delete video links
                 video_links_html += f" <a href='/topics/{topic.id}/videos/{video.id}/delete' style='color:red;'>Delete</a>"
            video_links_html += "</li>"
    else:
        video_links_html += "<li>No video links yet.</li>"
    video_links_html += "</ul>"

    add_note_link = ""
    add_document_link = ""
    add_video_link = ""
    if topic.owner_id == current_user.id: # Only owner can add content
        add_note_link = f"<p><a href='/topics/{topic.id}/notes/new'>Add New Note</a></p>"
        add_document_link = f"<p><a href='/topics/{topic.id}/documents/new'>Upload New Document</a></p>"
        add_video_link = f"<p><a href='/topics/{topic.id}/videos/new'>Add New Video Link</a></p>"

    return f"""
    <h1>Topic: {topic.name} ({'Private' if topic.is_private else 'Public'})</h1>
    <p>Owner: {topic.owner.username}</p>
    <hr>
    {notes_html}
    {add_note_link}
    <hr>
    {documents_html}
    {add_document_link}
    <hr>
    {video_links_html}
    {add_video_link}
    <hr>
    <p><a href="/topics">Back to Topics</a></p>
    <p><a href="/auth/logout">Logout</a></p>
    """

# --- Note Endpoints (CRUD) ---
@app.get("/topics/{topic_id}/notes/new", response_class=HTMLResponse)
async def new_note_page(topic_id: str, request: Request, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()
    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to add notes to this topic")

    return f"""
    <h1>Add New Note to Topic: {topic.name}</h1>
    <form id="noteForm">
        <label for="title">Title:</label><br>
        <input type="text" id="title" name="title" required><br><br>
        <label for="content">Content:</label><br>
        <textarea id="content" name="content" rows="10" cols="50"></textarea><br><br>
        <button type="submit">Save Note</button>
    </form>
    <div id="responseMessage"></div>
    <script>
        document.getElementById('noteForm').addEventListener('submit', async function(event) {{
            event.preventDefault();
            const title = document.getElementById('title').value;
            const content = document.getElementById('content').value;
            const responseMessage = document.getElementById('responseMessage');

            try {{
                const response = await fetch('/topics/{topic_id}/notes', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{ title, content }})
                }});

                const data = await response.json();
                if (response.ok) {{
                    responseMessage.textContent = 'Note created successfully! ID: ' + data.note_id;
                    responseMessage.style.color = 'green';
                    document.getElementById('title').value = '';
                    document.getElementById('content').value = '';
                    window.location.href = '/topics/{topic_id}'; // Redirect to topic detail
                }} else {{
                    responseMessage.textContent = 'Error: ' + (data.detail || 'Failed to create note');
                    responseMessage.style.color = 'red';
                }}
            }} catch (error) {{
                responseMessage.textContent = 'Network error: ' + error.message;
                responseMessage.style.color = 'red';
            }}
        }});
    </script>
    <p><a href="/topics/{topic_id}">Back to Topic</a></p>
    """

@app.post("/topics/{topic_id}/notes", status_code=201)
async def create_note_for_topic(topic_id: str, note_data: NoteCreate, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to add notes to this topic")

    new_note = Note(title=note_data.title, content=note_data.content, topic_id=topic_id)
    db.add(new_note)
    await db.commit()
    await db.refresh(new_note)
    return {"message": "Note created successfully", "note_id": new_note.id}

@app.get("/topics/{topic_id}/notes/{note_id}", response_class=HTMLResponse)
async def get_note_detail(topic_id: str, note_id: str, request: Request, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Note).where(Note.id == note_id, Note.topic_id == topic_id)
    )
    note = result.scalar_one_or_none()

    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or (topic.is_private and topic.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to view this note")

    return f"""
    <h1>Note: {note.title}</h1>
    <p><strong>Topic:</strong> <a href="/topics/{topic_id}">{topic.name}</a></p>
    <p><strong>Created:</strong> {note.created_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><strong>Last Updated:</strong> {note.updated_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
    <hr>
    <p>{note.content.replace('\\n', '<br>') if note.content else ''}</p>
    <hr>
    <p><a href="/topics/{topic_id}">Back to Topic</a></p>
    <p><a href="/auth/logout">Logout</a></p>
    """

@app.get("/topics/{topic_id}/notes/{note_id}/delete", response_class=HTMLResponse)
async def confirm_delete_note_page(topic_id: str, note_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Note).where(Note.id == note_id, Note.topic_id == topic_id))
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()
    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this note")

    return f"""
    <h1>Confirm Delete Note: {note.title}</h1>
    <p>Are you sure you want to delete this note? This action cannot be undone.</p>
    <form action="/topics/{topic_id}/notes/{note_id}" method="post">
        <input type="hidden" name="method" value="DELETE">
        <button type="submit" style="color:red;">Yes, Delete Note</button>
    </form>
    <p><a href="/topics/{topic_id}/notes/{note_id}">Cancel</a> | <a href="/topics/{topic_id}">Back to Topic</a></p>
    """

@app.post("/topics/{topic_id}/notes/{note_id}")
async def handle_delete_note_form(topic_id: str, note_id: str, method: str = Form(...), current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    if method == "DELETE":
        return await delete_note(topic_id, note_id, current_user, db)
    raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.delete("/topics/{topic_id}/notes/{note_id}", status_code=204)
async def delete_note(topic_id: str, note_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Note).where(Note.id == note_id, Note.topic_id == topic_id))
    note = result.scalar_one_or_none()

    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this note")

    await db.delete(note)
    await db.commit()
    return Response(status_code=204)


# --- Document Endpoints (Upload/Download/Delete) ---
@app.get("/topics/{topic_id}/documents/new", response_class=HTMLResponse)
async def new_document_page(topic_id: str, request: Request, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()
    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to upload documents to this topic")

    return f"""
    <h1>Upload New Document to Topic: {topic.name}</h1>
    <form id="documentForm" enctype="multipart/form-data">
        <label for="file">Choose File:</label><br>
        <input type="file" id="file" name="file" required><br><br>
        <button type="submit">Upload Document</button>
    </form>
    <div id="responseMessage"></div>
    <script>
        document.getElementById('documentForm').addEventListener('submit', async function(event) {{
            event.preventDefault();
            const fileInput = document.getElementById('file');
            const file = fileInput.files[0];
            const responseMessage = document.getElementById('responseMessage');

            if (!file) {{
                responseMessage.textContent = 'Please select a file to upload.';
                responseMessage.style.color = 'red';
                return;
            }}

            const formData = new FormData();
            formData.append('file', file);

            try {{
                const response = await fetch('/topics/{topic_id}/documents', {{
                    method: 'POST',
                    body: formData
                }});

                const data = await response.json();
                if (response.ok) {{
                    responseMessage.textContent = 'Document uploaded successfully! ID: ' + data.document_id;
                    responseMessage.style.color = 'green';
                    fileInput.value = ''; // Clear file input
                    window.location.href = '/topics/{topic_id}'; // Redirect to topic detail
                }} else {{
                    responseMessage.textContent = 'Error: ' + (data.detail || 'Failed to upload document');
                    responseMessage.style.color = 'red';
                }}
            }} catch (error) {{
                responseMessage.textContent = 'Network error: ' + error.message;
                responseMessage.style.color = 'red';
            }}
        }});
    </script>
    <p><a href="/topics/{topic_id}">Back to Topic</a></p>
    """


@app.post("/topics/{topic_id}/documents", status_code=201)
async def upload_document_for_topic(topic_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to upload documents to this topic")

    document_id = str(uuid.uuid4())
    # S3 key structure: user_id/topic_id/document_id-filename
    key = f"{current_user.id}/{topic_id}/{document_id}-{file.filename}"

    try:
        file_content = await file.read() # Read file content into memory
        s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=file_content, ContentType=file.content_type)
        print(f"Uploaded {file.filename} to {key}")

        new_document = Document(
            filename=file.filename,
            s3_key=key,
            file_size_bytes=str(len(file_content)), # Store size as string
            content_type=file.content_type,
            topic_id=topic_id
        )
        db.add(new_document)
        await db.commit()
        await db.refresh(new_document)
        return {"message": "Upload successful", "document_id": new_document.id}
    except Exception as e:
        print(f"Error uploading file to MinIO: {e}")
        raise HTTPException(status_code=500, detail=f"File upload failed: {e}")

@app.get("/topics/{topic_id}/documents/{document_id}/download") # This is an API endpoint, not a page
async def download_document(topic_id: str, document_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Document).where(Document.id == document_id, Document.topic_id == topic_id)
    )
    document = result.scalar_one_or_none()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or (topic.is_private and topic.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to download this document")

    try:
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': document.s3_key},
            ExpiresIn=300
        )
        return JSONResponse({"url": presigned_url})
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        raise HTTPException(status_code=500, detail="Could not generate download link")


@app.get("/topics/{topic_id}/documents/{document_id}/delete", response_class=HTMLResponse)
async def confirm_delete_document_page(topic_id: str, document_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.id == document_id, Document.topic_id == topic_id))
    document = result.scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()
    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this document")

    return f"""
    <h1>Confirm Delete Document: {document.filename}</h1>
    <p>Are you sure you want to delete this document? This action cannot be undone and will delete it from storage.</p>
    <form action="/topics/{topic_id}/documents/{document_id}" method="post">
        <input type="hidden" name="method" value="DELETE">
        <button type="submit" style="color:red;">Yes, Delete Document</button>
    </form>
    <p><a href="/topics/{topic_id}">Cancel</a></p>
    """

@app.post("/topics/{topic_id}/documents/{document_id}")
async def handle_delete_document_form(topic_id: str, document_id: str, method: str = Form(...), current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    if method == "DELETE":
        return await delete_document(topic_id, document_id, current_user, db)
    raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.delete("/topics/{topic_id}/documents/{document_id}", status_code=204)
async def delete_document(topic_id: str, document_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.id == document_id, Document.topic_id == topic_id))
    document = result.scalar_one_or_none()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this document")

    try:
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=document.s3_key)
        print(f"Deleted S3 object: {document.s3_key}")
    except Exception as e:
        print(f"Error deleting S3 object {document.s3_key}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file from storage: {e}")

    await db.delete(document)
    await db.commit()
    return Response(status_code=204)


# --- Video Link Endpoints (CRUD) ---
@app.get("/topics/{topic_id}/videos/new", response_class=HTMLResponse)
async def new_video_link_page(topic_id: str, request: Request, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = result.scalar_one_or_none()
    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to add video links to this topic")

    return f"""
    <h1>Add New Video Link to Topic: {topic.name}</h1>
    <form id="videoLinkForm">
        <label for="title">Title:</label><br>
        <input type="text" id="title" name="title" required><br><br>
        <label for="url">Video URL:</label><br>
        <input type="url" id="url" name="url" required><br><br>
        <label for="description">Description (optional):</label><br>
        <textarea id="description" name="description" rows="5" cols="50"></textarea><br><br>
        <button type="submit">Add Video Link</button>
    </form>
    <div id="responseMessage"></div>
    <script>
        document.getElementById('videoLinkForm').addEventListener('submit', async function(event) {{
            event.preventDefault();
            const title = document.getElementById('title').value;
            const url = document.getElementById('url').value;
            const description = document.getElementById('description').value;
            const responseMessage = document.getElementById('responseMessage');

            try {{
                const response = await fetch('/topics/{topic_id}/videos', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{ title, url, description }})
                }});

                const data = await response.json();
                if (response.ok) {{
                    responseMessage.textContent = 'Video link added successfully! ID: ' + data.video_link_id;
                    responseMessage.style.color = 'green';
                    document.getElementById('title').value = '';
                    document.getElementById('url').value = '';
                    document.getElementById('description').value = '';
                    window.location.href = '/topics/{topic_id}'; // Redirect to topic detail
                }} else {{
                    responseMessage.textContent = 'Error: ' + (data.detail || 'Failed to add video link');
                    responseMessage.style.color = 'red';
                }}
            }} catch (error) {{
                responseMessage.textContent = 'Network error: ' + error.message;
                responseMessage.style.color = 'red';
            }}
        }});
    </script>
    <p><a href="/topics/{topic_id}">Back to Topic</a></p>
    """

@app.post("/topics/{topic_id}/videos", status_code=201)
async def create_video_link_for_topic(topic_id: str, video_data: VideoLinkCreate, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to add video links to this topic")

    new_video_link = VideoLink(
        title=video_data.title,
        url=str(video_data.url), # Convert Pydantic's HttpUrl to str
        description=video_data.description,
        topic_id=topic_id
    )
    db.add(new_video_link)
    await db.commit()
    await db.refresh(new_video_link)
    return {"message": "Video link added successfully", "video_link_id": new_video_link.id}


@app.get("/topics/{topic_id}/videos/{video_link_id}/delete", response_class=HTMLResponse)
async def confirm_delete_video_link_page(topic_id: str, video_link_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VideoLink).where(VideoLink.id == video_link_id, VideoLink.topic_id == topic_id))
    video_link = result.scalar_one_or_none()
    if not video_link:
        raise HTTPException(status_code=404, detail="Video link not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()
    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this video link")

    return f"""
    <h1>Confirm Delete Video Link: {video_link.title}</h1>
    <p>Are you sure you want to delete this video link? This action cannot be undone.</p>
    <form action="/topics/{topic_id}/videos/{video_link_id}" method="post">
        <input type="hidden" name="method" value="DELETE">
        <button type="submit" style="color:red;">Yes, Delete Video Link</button>
    </form>
    <p><a href="/topics/{topic_id}">Cancel</a></p>
    """

@app.post("/topics/{topic_id}/videos/{video_link_id}")
async def handle_delete_video_link_form(topic_id: str, video_link_id: str, method: str = Form(...), current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)): # Changed _method to method
    if method == "DELETE": # Changed _method to method
        return await delete_video_link(topic_id, video_link_id, current_user, db)
    raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.delete("/topics/{topic_id}/videos/{video_link_id}", status_code=204)
async def delete_video_link(topic_id: str, video_link_id: str, current_user: User = Depends(get_current_user_obj), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VideoLink).where(VideoLink.id == video_link_id, VideoLink.topic_id == topic_id))
    video_link = result.scalar_one_or_none()

    if not video_link:
        raise HTTPException(status_code=404, detail="Video link not found")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    topic = topic_result.scalar_one_or_none()

    if not topic or topic.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this video link")

    await db.delete(video_link)
    await db.commit()
    return Response(status_code=204)

# --- Health Check (can keep this for basic monitoring) ---
@app.get("/health")
def health_check():
    return {"status": "running"}

# --- Run the application (for development) ---
if __name__ == "__main__":
    import uvicorn
    print(f"Keycloak Issuer: {KEYCLOAK_ISSUER}")
    print(f"Keycloak Client ID: {KEYCLOAK_CLIENT_ID}")

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)