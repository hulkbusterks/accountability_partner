import json
from authlib.jose import jwt
from authlib.common.security import generate_token
from authlib.jose import JsonWebKey
import os
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware # For session management
from authlib.integrations.starlette_client import OAuth # For OIDC client
from authlib.common.security import generate_token # For CSRF state parameter
from dotenv import load_dotenv

from authlib.jose.errors import InvalidClaimError # Keep this import for general error handling
from starlette.responses import RedirectResponse
from starlette.config import Config

# --- Your existing configuration and OAuth setup (ensure these are correctly defined) ---
config = Config('.env') # Assuming you have a .env file for configuration
oauth = OAuth(config)

KEYCLOAK_ISSUER = config('KEYCLOAK_ISSUER', default='http://localhost:8080/realms/myrealm')
KEYCLOAK_CLIENT_ID = config('KEYCLOAK_CLIENT_ID', default='accountability_partner')
KEYCLOAK_CLIENT_SECRET = config('KEYCLOAK_CLIENT_SECRET', default='your_client_secret') # Make sure this matches your Keycloak client secret

oauth.register(
    name='keycloak',
    client_id=KEYCLOAK_CLIENT_ID,
    client_secret=KEYCLOAK_CLIENT_SECRET,
    server_metadata_url=f'{KEYCLOAK_ISSUER}/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid profile email'},
)

# Global cache for JWKs (still useful if you need to manually verify other JWTs later,
# but for the ID Token in OIDC callback, Authlib often handles it)
jwks_cache = {}

async def get_keycloak_jwks():
    """Fetches and caches Keycloak's JSON Web Key Set."""
    global jwks_cache
    if not jwks_cache: # Only fetch if cache is empty
        print("\n--- Fetching JWKS from Keycloak ---")
        metadata = await oauth.keycloak.load_server_metadata()
        jwks_uri = metadata['jwks_uri']
        print(f"JWKS URI: {jwks_uri}")
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            jwks_data = resp.json()
        print(f"Raw JWKS data received (truncated for brevity): {json.dumps(jwks_data, indent=2)[:500]}...")

        if not jwks_data.get('keys'):
            print("WARNING: 'keys' array not found in JWKS response or is empty.")
            return {}

        for i, key_data_item in enumerate(jwks_data.get('keys', [])):
            print(f"Processing key {i}:")
            print(f"  Type of key_data_item: {type(key_data_item)}")
            print(f"  Content of key_data_item: {key_data_item}")

            try:
                if key_data_item.get('use') == 'sig':
                    jwk = JsonWebKey.import_key(key_data_item)
                    kid = jwk.as_dict().get('kid')
                    if kid:
                        jwks_cache[kid] = jwk
                        print(f"  Successfully imported and cached JWK with KID: {kid}")
                        print(f"  Imported JWK as_dict: {jwk.as_dict()}")
                        print(f"  JWK 'e' parameter: {jwk.as_dict().get('e')}")
                        print(f"  JWK 'n' parameter: {jwk.as_dict().get('n')}")
                    else:
                        print(f"  Warning: JWK {i} imported but has no 'kid': {jwk.as_dict()}")
                else:
                    print(f"  Skipping key {i} because 'use' is not 'sig' or missing.")
            except ValueError as e:
                print(f"  Error importing JWK {i} (ValueError): {e} - Data: {key_data_item}")
            except Exception as e:
                print(f"  Unexpected error in JWKS import loop for key {i}: {e} - Data: {key_data_item}")
        print("--- JWKS Fetching Complete ---")
    else:
        print("JWKS already cached. Skipping fetch.")
    return jwks_cache

# --- FastAPI App Initialization ---
app = FastAPI()
load_dotenv()

app.add_middleware(SessionMiddleware, secret_key=os.urandom(32)) # Replace with strong secret

# --- User Management (Simple in-memory for example, use DB in prod) ---

async def get_current_user_id(request: Request):
    """
    Dependency to get the current authenticated user's ID from the session.
    """
    user_id = request.session.get('user_id')
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# --- FastAPI Routes ---

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    user_id = request.session.get('user_id')
    if user_id:
        return f"""
        <h1>Welcome, User {user_id}!</h1>
        <p><a href="/notes">Go to your notes</a></p>
        <p><a href="/auth/logout">Logout</a></p>
        """
    return """
    <h1>Welcome to Notes App!</h1>
    <p><a href="/auth/login">Login with Keycloak</a></p>
    """

@app.get("/auth/login")
async def login(request: Request):
    """Initiates the Keycloak login flow."""
    redirect_uri = request.url_for('auth_callback') # URL of your /auth/callback endpoint
    print(f"Redirecting to: {redirect_uri}")
    return await oauth.keycloak.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        # authorize_access_token should handle the token exchange AND ID Token validation
        # It typically returns a dictionary containing access_token, refresh_token,
        # id_token (raw string), and userinfo (parsed ID Token claims).
        token = await oauth.keycloak.authorize_access_token(request)
        print("\n--- Received token from Keycloak (Initial) ---")
        print(token)
        print("-----------------------------------")

        userinfo = token.get('userinfo')
        if not userinfo:
            # This indicates Authlib itself failed to validate the ID Token
            # or the 'openid' scope was not granted.
            raise ValueError("User info (parsed ID Token claims) not found in token response. "
                             "ID Token validation may have failed or 'openid' scope is missing.")

        print("--- Successfully obtained User Info from Authlib's authorize_access_token ---")
        print(userinfo)
        print("------------------------------------------------------------------")

        request.session['user_id'] = userinfo.get('sub')
        request.session['user_name'] = userinfo.get('preferred_username', userinfo.get('name'))

        return RedirectResponse(url="/notes")

    except Exception as e:
        print(f"Caught exception: {e}")
        # Authlib's authorize_access_token should raise more specific exceptions
        # (e.g., OAuthError) if validation fails.
        raise HTTPException(status_code=400, detail=f"Authentication failed: {e}")


@app.get("/auth/logout")
async def logout(request: Request):
    """Logs out the user and clears the session."""
    if 'user_id' in request.session:
        del request.session['user_id']
    if 'user_name' in request.session:
        del request.session['user_name']

    # Optionally, redirect to Keycloak's logout endpoint for single sign-out
    # This requires Keycloak's front-channel or back-channel logout to be configured
    # Keycloak logout URL often looks like: {KEYCLOAK_ISSUER}/protocol/openid-connect/logout?redirect_uri={your_app_url}
    # For simplicity, we just clear the local session here.
    return RedirectResponse(url="/")

@app.get("/notes", response_class=HTMLResponse)
async def get_user_notes(request: Request, user_id: str = Depends(get_current_user_id)):
    """A protected endpoint to display user's notes."""
    user_name = request.session.get('user_name', user_id)
    return f"""
    <h1>Your Private Notes, {user_name}!</h1>
    <p>This page is protected and requires authentication.</p>
    <p>Your unique ID from Keycloak: <strong>{user_id}</strong></p>
    <p>Here you would list notes fetched from MinIO/database for user {user_id}.</p>
    <p><a href="/auth/logout">Logout</a></p>
    """

# --- Run the application (for development) ---
if __name__ == "__main__":
    import uvicorn
    print(f"Keycloak Issuer: {KEYCLOAK_ISSUER}")
    print(f"Keycloak Client ID: {KEYCLOAK_CLIENT_ID}")

    uvicorn.run(app, host="0.0.0.0", port=8000)