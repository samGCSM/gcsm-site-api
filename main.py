# main.py
from fastapi.middleware.cors import CORSMiddleware
from site_api import app as fastapi_app  # <-- import the ONE FastAPI app

app = fastapi_app

# For now, be very explicit with allowed origins
origins = [
    "https://gcsm-site-specific.lovable.app",
    "http://localhost:5173",
    "https://sitespecific-production.up.railway.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,  # keep this False while debugging
    allow_methods=["*"],
    allow_headers=["*"],
)








