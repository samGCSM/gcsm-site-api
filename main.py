from fastapi.middleware.cors import CORSMiddleware

# Import the existing FastAPI app from site_api.py
from site_api import app as fastapi_app

app = fastapi_app

# Add CORS middleware so Lovable + local dev can call it
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://dctxiunpvdungcwwqliw.lovableproject.com",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
