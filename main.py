from fastapi.middleware.cors import CORSMiddleware

# Import the existing FastAPI app from site_api.py
from site_api import app as fastapi_app

app = fastapi_app

# Add CORS middleware so Lovable + local dev can call it
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://dctxiunpvdungcwwqliw.lovableproject.com",
	"https://d8e5e12f-4b06-4dca-9816-62fb70779a59.lovableproject.com",
        "http://localhost:5173",
        "http://localhost:8080",
	"https://gcsm-site-specific.lovable.app/",
	"https://gcsm-site-specific.lovable.app",
    ],
    allow_origin_regex=r"https://.*\.(lovable\.app|lovableproject\.com)",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)



