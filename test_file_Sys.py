from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import boto3
import uuid
import os

app = FastAPI()

S3_ENDPOINT = "http://localhost:9000"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
BUCKET_NAME = "mybucket"

file_metadata = {}

s3 = boto3.client(
    's3',
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    region_name='us-east-1'
)

try:
    s3.head_bucket(Bucket=BUCKET_NAME)
except:
    s3.create_bucket(Bucket=BUCKET_NAME)


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    key = f"user_123/{file_id}-{file.filename}"

    s3.upload_fileobj(file.file, BUCKET_NAME, key)

    file_metadata[file_id] = {
        "filename": file.filename,
        "s3_key": key,
        "owner": "user_123",
    }

    return {"file_id": file_id, "message": "Upload successful"}


@app.get("/download/{file_id}")
def download_file(file_id: str):
    meta = file_metadata.get(file_id)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")

    presigned_url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': BUCKET_NAME, 'Key': meta["s3_key"]},
        ExpiresIn=300  
    )

    return JSONResponse({"url": presigned_url})

@app.delete("/delete/{file_id}")
def delete_file(file_id: str):
    meta = file_metadata.pop(file_id, None)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")

    # Delete from storage
    s3.delete_object(Bucket=BUCKET_NAME, Key=meta["s3_key"])

    return {"message": "File deleted successfully"}


@app.get("/")
def health_check():
    return {"status": "running"}
