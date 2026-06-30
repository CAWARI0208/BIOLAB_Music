import os
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import urllib.request
import urllib.parse
import re
import datetime
import wikipedia
import json
import html
import traceback
from youtube_transcript_api import YouTubeTranscriptApi
from typing import List, Optional

from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel

# --- 1. 資料庫設定 ---
raw_url = os.getenv("MYSQL_URL")
# SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:0000@localhost/music_db"

if raw_url and raw_url.startswith("mysql://"):
    SQLALCHEMY_DATABASE_URL = raw_url.replace("mysql://", "mysql+pymysql://", 1)
else:
    SQLALCHEMY_DATABASE_URL = raw_url

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Song(Base):
    __tablename__ = "song_list"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255))
    name = Column(String(100))
    type = Column(String(50))
    language = Column(String(50))
    duration = Column(String(20))
    release = Column(String(20))
    URL = Column(Text)

Base.metadata.create_all(bind=engine)

# --- 2. Pydantic 模型 ---
class SongCreate(BaseModel):
    title: str
    name: str
    type: str
    language: str
    duration: str
    release: str
    URL: str

class YTResultSchema(BaseModel):
    title: str
    name: str
    duration: str
    release: str
    URL: str
    thumbnail: str
    language: str
    v_id: str

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 3. 核心邏輯：偵測語言與種類 (字幕優先版) ---
def get_advanced_metadata(v_id, title):
    res = {"language": "", "type": "Pop"}
    # 1. 關鍵字清洗
    clean_q = re.sub(r'(?i)official|video|audio|lyrics|4k|hd|music|mv|【.*】|\[.*\]', '', title).strip()

    # 定義偵測函數，避免重複寫兩次相同的邏輯
    def analyze_content(text_summary):
        detected = {"lang": None, "genre": "Pop"}
        text_summary = text_summary.lower()

        # --- A. 語言判定 (優先度最高) ---
        lang_keywords = {
            "Spanish": ["spanish", "puerto rico", "latin pop", "reggaeton"],
            "Japanese": ["japanese", "j-pop", "japan", "anime"],
            "Korean": ["korean", "k-pop", "south korea", "hangul"],
            "Chinese": ["chinese", "mandarin", "cantonese", "taiwan", "hong kong", "mandopop"]
        }
        for lang, keys in lang_keywords.items():
            if any(k in text_summary for k in keys):
                detected["lang"] = lang
                break

        # --- B. 強勢攔截：地區型流派 (直接 Return 防止被 Rock 蓋掉) ---
        # 如果摘要提到這些強勢字眼，直接判定，不往下走
        if any(k in text_summary for k in ["j-pop", "jpop", "japanese pop", "kenshi yonezu"]):
            detected["genre"] = "J-Pop"
            return detected
        if any(k in text_summary for k in ["k-pop", "kpop", "korean pop", "south korean boy band", "south korean girl group"]):
            detected["genre"] = "K-Pop"
            return detected
        
        mandopop_keys = [
            "mandopop", "c-pop", "chinese pop", "cantopop", 
            "taiwanese pop", "hong kong pop", "mainland chinese pop",
            "華語流行", "台灣流行", "粵語流行"
        ]
        if any(k in text_summary for k in ["mandopop", "c-pop", "chinese pop"]):
            detected["genre"] = "Mandopop"
            return detected

        # --- C. 次要判定：一般音樂風格 (如果上面沒攔截到才跑這裡) ---
        style_genres = {
            "Rock": ["rock", "punk", "metal"],
            "Hip Hop": ["hip hop", "rap", "trap"],
            "R&B": ["r&b", "soul"],
            "EDM": ["edm", "electronic", "dance"],
            "Pop": ["pop", "dance-pop"]
        }
        for g, keys in style_genres.items():
            if any(k in text_summary for k in keys):
                detected["genre"] = g
                break
                
        return detected

    # --- 第一層：英文維基百科 ---
    try:
        wikipedia.set_lang("en")
        search_results = wikipedia.search(f"{clean_q} song", results=1)
        if search_results:
            page = wikipedia.page(search_results[0], auto_suggest=False)
            info = analyze_content(page.summary)
            res["language"] = info["lang"] if info["lang"] else "English"
            res["type"] = info["genre"]
            return res
    except: pass

    # --- 第二層：中文維基百科 ---
    try:
        wikipedia.set_lang("zh")
        search_results = wikipedia.search(clean_q, results=1)
        if search_results:
            page = wikipedia.page(search_results[0], auto_suggest=False)
            info = analyze_content(page.summary)
            # 中文維基若沒對到語言，預設就是 Chinese
            res["language"] = info["lang"] if info["lang"] else "Chinese"
            res["type"] = info["genre"]
            return res
    except: pass

    # --- 第三層：YouTube 字幕 ---
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(v_id)
        codes = [t.language_code[:2] for t in transcript_list]
        lang_map = {'en': 'English', 'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean', 'es': 'Spanish'}
        for code, name in lang_map.items():
            if code in codes:
                res["language"] = name
                return res
    except: pass

    # --- 第四層：標題正則保底 ---
    if re.search(r'[\u4e00-\u9fff]', title): res["language"] = "Chinese"
    elif re.search(r'[\u3040-\u30ff]', title): res["language"] = "Japanese"
    elif re.search(r'[\uac00-\ud7af]', title): res["language"] = "Korean"
    else: res["language"] = "English"
    
    return res

# --- 4. 路由設定 ---
@app.get("/youtube_search", response_model=List[YTResultSchema])
async def search_youtube(query: str = Query(..., description="輸入歌名與歌手")):
    try:
        current_year = datetime.datetime.now().year
        search_keyword = f"{query} official"
        encoded_query = urllib.parse.quote(search_keyword)
        url = f"https://www.youtube.com/results?search_query={encoded_query}"
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
        req = urllib.request.Request(url, headers=headers)
        
        with urllib.request.urlopen(req) as response:
            html_content = response.read().decode('utf-8', errors='ignore')

        video_sections = re.findall(r'"videoRenderer":\{(.*?)\}(?=,"searchVideoResultEntity"|,"shortBylineText"|,"trackingParams")', html_content)
        processed_list = []
        seen_ids = set()
        
        for section in video_sections:
            id_match = re.search(r'"videoId":"([^"]+)"', section)
            title_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', section)
            duration_match = re.search(r'"lengthText":\{.*?"simpleText":"([^"]+)"\}', section)
            time_match = re.search(r'"publishedTimeText":\{"simpleText":"([^"]+)"\}', section)

            if id_match and title_match:
                v_id = id_match.group(1)
                v_title = title_match.group(1)
                if v_id in seen_ids: continue
                
                # 安全解碼標題
                try:
                    clean_title = json.loads(f'"{v_title}"')
                    clean_title = html.unescape(clean_title)
                except:
                    clean_title = html.unescape(v_title).replace(r'\u0026', '&')

                v_duration = duration_match.group(1) if duration_match else "--:--"
                v_time_text = time_match.group(1) if time_match else "N/A"
                
                # --- 智慧年份計算 ---
                release_year = str(current_year)
                # 1. 優先匹配 4 位數字 (絕對年份)
                abs_year_match = re.search(r'(\d{4})', v_time_text)
                if abs_year_match:
                    release_year = abs_year_match.group(1)
                else:
                    # 2. 匹配相對年份 (16 年前 / 16 years ago)
                    rel_year_match = re.search(r'(\d+)\s*(?:年|year)', v_time_text)
                    if rel_year_match:
                        years_ago = int(rel_year_match.group(1))
                        release_year = str(current_year - years_ago)

                processed_list.append({
                    "title": clean_title, 
                    "name": "", 
                    "duration": v_duration, 
                    "release": release_year, 
                    "URL": f"https://www.youtube.com/watch?v={v_id}",
                    "thumbnail": f"https://img.youtube.com/vi/{v_id}/mqdefault.jpg",
                    "language": "Chinese" if re.search(r'[\u4e00-\u9fff]', clean_title) else "English",
                    "v_id": v_id
                })
                seen_ids.add(v_id)

            if len(processed_list) >= 6: break
        return processed_list
    except Exception:
        traceback.print_exc()
        return []

@app.get("/detect_lang")
def detect_lang(v_id: str, title: str):
    return get_advanced_metadata(v_id, title)

# --- 5. 資料庫 CRUD ---
@app.get("/songs")
def read_songs(db: Session = Depends(get_db)):
    return db.query(Song).all()

@app.post("/songs")
def create_song(song: SongCreate, db: Session = Depends(get_db)):
    db_song = Song(**song.model_dump())
    db.add(db_song)
    db.commit()
    db.refresh(db_song)
    return db_song

@app.delete("/songs/{song_id}")
def delete_song(song_id: int, db: Session = Depends(get_db)):
    db_song = db.query(Song).filter(Song.id == song_id).first()
    if not db_song: raise HTTPException(status_code=404, detail="找不到這首歌")
    db.delete(db_song)
    db.commit()
    return {"message": "成功刪除"}

@app.put("/songs/{song_id}")
def replace_song(song_id: int, song: SongCreate, db: Session = Depends(get_db)):
    db_song = db.query(Song).filter(Song.id == song_id).first()
    if not db_song:
        raise HTTPException(status_code=404, detail="找不到欲更改的歌曲")
    
    # 將新資料直接覆蓋舊資料的所有欄位
    for key, value in song.model_dump().items():
        setattr(db_song, key, value)
    
    db.commit()
    db.refresh(db_song)
    return db_song

@app.patch("/songs/{song_id}")
def update_song(song_id: int, data: dict, db: Session = Depends(get_db)):
    db_song = db.query(Song).filter(Song.id == song_id).first()
    if not db_song: raise HTTPException(status_code=404, detail="找不到這首歌")
    for key, value in data.items(): setattr(db_song, key, value)
    db.commit()
    db.refresh(db_song)
    return db_song

if __name__ == "__main__":
    import uvicorn
    # 抓取雲端指定的 Port，否則預設 8000
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)