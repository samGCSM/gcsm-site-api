# main.py
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://gcsm-site-specific.lovable.app",
        "http://localhost:5173",
    ],
    allow_origin_regex=r"https://.*\.(lovable\.app|lovableproject\.com)",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)







