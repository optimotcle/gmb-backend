from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GOOGLE_PLACES_API_KEY = os.environ.get('GOOGLE_PLACES_API_KEY', '')
GOOGLE_PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

class PlaceSearchRequest(BaseModel):
    keyword: str
    city: str
    business_name: Optional[str] = None
    max_results: int = 40
    language: str = "fr"

class PlaceResult(BaseModel):
    id: str
    name: str
    address: str
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    website: Optional[str] = None
    phone: Optional[str] = None
    types: List[str] = []
    is_verified: bool = False
    rank: int = 0
    photo_url: Optional[str] = None

class SearchResponse(BaseModel):
    places: List[PlaceResult]
    total_results: int
    target_business_rank: Optional[int] = None
    target_business: Optional[PlaceResult] = None
    search_query: str
    city: str

def normalize_city_name(city: str) -> str:
    city_lower = city.lower().strip()
    abbreviations = {'st-': 'saint-', 'st ': 'saint ', 'ste-': 'sainte-', 'ste ': 'sainte ', 'mtl': 'montréal', 'qc': 'québec'}
    normalized = city_lower
    for abbrev, full in abbreviations.items():
        if normalized.startswith(abbrev):
            normalized = full + normalized[len(abbrev):]
        normalized = normalized.replace(f' {abbrev}', f' {full}')
    city_expansions = {
        'sainte-marthe': 'Sainte-Marthe-sur-le-Lac', 'ste-marthe': 'Sainte-Marthe-sur-le-Lac',
        'deux-montagnes': 'Deux-Montagnes', 'saint-eustache': 'Saint-Eustache', 'st-eustache': 'Saint-Eustache',
        'laval': 'Laval', 'montréal': 'Montréal', 'montreal': 'Montréal', 'longueuil': 'Longueuil',
        'brossard': 'Brossard', 'terrebonne': 'Terrebonne', 'blainville': 'Blainville', 'repentigny': 'Repentigny',
        'saint-jerome': 'Saint-Jérôme', 'st-jerome': 'Saint-Jérôme', 'mirabel': 'Mirabel',
        'boisbriand': 'Boisbriand', 'rosemere': 'Rosemère', 'mascouche': 'Mascouche',
    }
    for key, value in city_expansions.items():
        if normalized == key or normalized.startswith(key):
            return value
    return ' '.join(word.capitalize() for word in normalized.split())

def check_gmb_verification(place_data: dict) -> bool:
    indicators = [
        place_data.get("businessStatus") == "OPERATIONAL",
        "nationalPhoneNumber" in place_data or "formattedPhoneNumber" in place_data,
        len(place_data.get("photos", [])) > 0,
        place_data.get("userRatingCount", 0) > 5,
        "websiteUri" in place_data,
        "regularOpeningHours" in place_data,
    ]
    return (sum(indicators) / len(indicators)) >= 0.5 if indicators else False

def get_photo_url(photo_name: str) -> str:
    if not photo_name or not GOOGLE_PLACES_API_KEY:
        return ""
    return f"https://places.googleapis.com/v1/{photo_name}/media?maxHeightPx=400&maxWidthPx=400&key={GOOGLE_PLACES_API_KEY}"

@app.get("/api")
async def root():
    return {"message": "Optimotcle GMB Checker API", "status": "ok"}

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "google_api_configured": bool(GOOGLE_PLACES_API_KEY)}

@app.post("/api/places/search", response_model=SearchResponse)
async def search_places(request: PlaceSearchRequest):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=500, detail="Google Places API key not configured")
    
    normalized_city = normalize_city_name(request.city)
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.websiteUri,places.nationalPhoneNumber,places.types,places.photos,places.businessStatus,places.regularOpeningHours"
    }
    
    all_places_raw = []
    seen_place_ids = set()
    search_queries = [f"{request.keyword} à {normalized_city}", f"{request.keyword} {normalized_city}", f"{request.keyword} près de {normalized_city}"]
    keyword_words = request.keyword.split()
    if len(keyword_words) > 1:
        main_keyword = keyword_words[0] if len(keyword_words[0]) > 3 else ' '.join(keyword_words[:2])
        search_queries.append(f"{main_keyword} à {normalized_city}")
    
    async with httpx.AsyncClient() as http_client:
        for text_query in search_queries:
            if len(all_places_raw) >= request.max_results:
                break
            try:
                response = await http_client.post(GOOGLE_PLACES_SEARCH_URL, json={"textQuery": text_query, "maxResultCount": 20, "languageCode": request.language}, headers=headers, timeout=30.0)
                if response.status_code == 200:
                    for place in response.json().get("places", []):
                        place_id = place.get("id", "")
                        if place_id and place_id not in seen_place_ids:
                            seen_place_ids.add(place_id)
                            all_places_raw.append(place)
            except Exception:
                continue
    
    places = []
    target_business = None
    target_rank = None
    
    for index, place in enumerate(all_places_raw[:request.max_results]):
        photo_url = get_photo_url(place["photos"][0].get("name", "")) if place.get("photos") else ""
        place_result = PlaceResult(
            id=place.get("id", ""), name=place.get("displayName", {}).get("text", "Inconnu"),
            address=place.get("formattedAddress", ""), rating=place.get("rating"),
            reviews_count=place.get("userRatingCount"), website=place.get("websiteUri"),
            phone=place.get("nationalPhoneNumber"), types=place.get("types", []),
            is_verified=check_gmb_verification(place), rank=index + 1, photo_url=photo_url
        )
        places.append(place_result)
        
        if request.business_name and target_business is None:
            bn = request.business_name.lower().strip()
            pn = place_result.name.lower()
            for c in ['inc.', 'inc', 'ltd', 'ltée', 'enr.', 'enr', ',', '.']:
                bn = bn.replace(c, ' ')
                pn = pn.replace(c, ' ')
            bw = [w for w in bn.split() if len(w) > 2]
            is_match = bn in pn or pn in bn or (bw and all(w in pn for w in bw))
            if not is_match and bw:
                common = ['service', 'services', 'nettoyage', 'garage', 'restaurant', 'salon', 'boutique', 'centre', 'magasin', 'entretien', 'auto', 'commercial']
                for w in [x for x in bw if x not in common and len(x) > 3]:
                    if w in pn:
                        is_match = True
                        break
            if is_match:
                target_business = place_result
                target_rank = index + 1
    
    return SearchResponse(places=places, total_results=len(places), target_business_rank=target_rank, target_business=target_business, search_query=request.keyword, city=normalized_city)
