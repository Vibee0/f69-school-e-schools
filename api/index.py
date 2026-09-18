import base64
import json
import os
import time
import uuid
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

# ==========================================
# 1. БЕЗОПАСНЫЕ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# В коде НЕТ паролей. При необходимости они
# задаются в панели Render.com (Вкладка Environment)
# ==========================================
DEFAULT_USERNAME = os.environ.get("DEFAULT_USERNAME", "")
DEFAULT_PASSWORD = os.environ.get("DEFAULT_PASSWORD", "")

# Часовой пояс Беларуси
MINSK_TZ = timezone(timedelta(hours=3))

app = FastAPI(title="Schools.by Multi-User Proxy")


# ==========================================
# 2. ДВИЖОК СИНХРОНИЗАЦИИ
# ==========================================
class ESchoolsBackend:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.auth_token: Optional[str] = None
        self.student_id: Optional[str] = None
        self.school_id: Optional[str] = None
        self.school_name: str = "Школа"
        self.class_id: Optional[str] = None
        self.class_name: str = "11А"
        self.first_name: str = "Ученик"
        self.last_name: str = ""
        self.father_name: str = ""
        self.last_login_time: float = 0

    def login(self):
        """Полная авторизация через OAuth2 РИОС"""
        print(f"[*] Авторизация пользователя {self.username} на e-schools.by...")
        start_url = "https://diary.e-schools.by/api/v1/admin/auth/login/student"
        resp = self.session.get(start_url)
        if resp.status_code != 200:
            raise Exception(f"Не удалось загрузить форму входа: HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "html.parser")
        payload = {}
        for hidden in soup.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            val = hidden.get("value", "")
            if name:
                payload[name] = val

        payload["Username"] = self.username
        payload["Input.Username"] = self.username
        payload["Password"] = self.password
        payload["Input.Password"] = self.password
        payload["RememberLogin"] = "true"
        payload["Input.RememberLogin"] = "true"
        payload["button"] = "login"
        payload["Input.Button"] = "login"

        login_resp = self.session.post(resp.url, data=payload, allow_redirects=True)

        data_uuid = None
        all_urls = [h.url for h in login_resp.history] + [login_resp.url]
        for u in all_urls:
            parsed = urllib.parse.urlparse(u)
            qs = urllib.parse.parse_qs(parsed.query)
            if "data" in qs:
                data_uuid = qs["data"][0]
                break
            if parsed.fragment and "?" in parsed.fragment:
                frag_qs = urllib.parse.parse_qs(parsed.fragment.split("?")[-1])
                if "data" in frag_qs:
                    data_uuid = frag_qs["data"][0]
                    break

        if not data_uuid:
            raise Exception("Неверный логин или пароль (data UUID не получен)")

        # Запрос данных профиля
        data_resp = self.session.get(f"https://diary.e-schools.by/api/v1/admin/auth/data_for_login/{data_uuid}")
        login_data = data_resp.json()
        self.student_id = login_data["profile_id"]
        self.school_id = login_data["schools"][0]["id"]
        self.school_name = login_data["schools"][0]["name"]

        # Обмен Base64 на JWT
        token_raw = f"{self.student_id}:{self.school_id}:student"
        token_b64 = base64.b64encode(token_raw.encode("utf-8")).decode("utf-8")

        auth_headers = {
            "Referer": "https://diary.e-schools.by/",
            "Origin": "https://diary.e-schools.by",
            "Cookie": f"usr_type=student; cvrf_id={data_uuid}"
        }
        self.session.cookies.set("usr_type", "student", domain="diary.e-schools.by")
        self.session.cookies.set("cvrf_id", data_uuid, domain="diary.e-schools.by")

        exchange_resp = self.session.get(f"https://diary.e-schools.by/api/v1/auth/login?token={token_b64}", headers=auth_headers)
        self.auth_token = exchange_resp.json().get("auth_token")
        self.last_login_time = time.time()

        self.fetch_user_meta()
        print(f"[+] Успешный вход: {self.first_name} {self.last_name}, класс {self.class_name}")

    def fetch_user_meta(self):
        """Определяет класс и имя ученика"""
        try:
            me_resp = self.session.get("https://diary.e-schools.by/api/v1/auth/me", headers=self.get_headers())
            if me_resp.status_code == 200:
                me_data = me_resp.json()
                self.first_name = me_data.get("first_name", "Ученик")
                self.last_name = me_data.get("last_name", "")
                self.father_name = me_data.get("middle_name", "")
        except Exception:
            pass

        try:
            weeks = self.fetch_weeks()
            now_ms = int(time.time() * 1000)
            target_week = weeks[0] if weeks else None
            for w in weeks:
                if w["start_ts"] <= now_ms <= w["end_ts"]:
                    target_week = w
                    break

            if target_week:
                dt_from = datetime.fromtimestamp(target_week["start_ts"] / 1000, tz=MINSK_TZ)
                dt_to = dt_from + timedelta(days=6)
                year_start = dt_from.year if dt_from.month >= 8 else dt_from.year - 1
                period_str = f"00000000-0000-{year_start}-{year_start + 1}-000000000000"

                url = f"https://diary.e-schools.by/api/v1/education/diary/schools/{self.school_id}/students/{self.student_id}/classes"
                headers = {**self.get_headers(), "Content-Type": "application/json"}
                payload = {
                    "from": dt_from.strftime("%d.%m.%Y"),
                    "to": dt_to.strftime("%d.%m.%Y"),
                    "school_period": period_str
                }
                c_resp = self.session.post(url, headers=headers, json=payload)
                if c_resp.status_code == 200:
                    classes = c_resp.json().get("classes", [])
                    if classes:
                        self.class_id = classes[0]["uuid"]
                        self.class_name = classes[0].get("class_number", "11А")
        except Exception as e:
            print(f"[*] Автоопределение класса: {e}")

    def ensure_auth(self):
        if not self.auth_token or (time.time() - self.last_login_time > 5400):
            self.login()

    def get_headers(self) -> dict:
        return {
            "Authorization": self.auth_token,
            "Cookie": "usr_type=student",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

    def fetch_weeks(self) -> list:
        self.ensure_auth()
        url = "https://diary.e-schools.by/api/v1/education/diary/time_activities/week_activities"
        resp = self.session.get(url, headers=self.get_headers())
        return resp.json() if resp.status_code == 200 else []

    def fetch_new_lessons(self, week_uuid: str) -> list:
        self.ensure_auth()
        class_id = self.class_id or "997b77ff-e26a-4a58-81cf-98390bfa5b41"
        url = (
            f"https://diary.e-schools.by/api/v1/education/diary/schools/{self.school_id}/"
            f"classes/{class_id}/students/{self.student_id}/lessons"
        )
        resp = self.session.get(url, headers=self.get_headers(), params={"week_activity_uuid": week_uuid})
        return resp.json() if resp.status_code == 200 else []


# Хранилище сессий пользователей: { "token": ESchoolsBackend }
user_sessions: Dict[str, ESchoolsBackend] = {}

# Сессия по умолчанию (если заданы переменные окружения)
default_backend = ESchoolsBackend(DEFAULT_USERNAME, DEFAULT_PASSWORD) if DEFAULT_USERNAME and DEFAULT_PASSWORD else None


def resolve_backend(request: Request) -> ESchoolsBackend:
    """Определяет пользователя по токену авторизации"""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").replace("Token ", "").strip()

    if not token:
        token = request.cookies.get("session_token", "")

    if token and token in user_sessions:
        return user_sessions[token]

    if default_backend:
        return default_backend

    raise Exception("Пользователь не авторизован")


@app.on_event("startup")
def startup():
    if default_backend:
        try:
            default_backend.login()
        except Exception as e:
            print(f"[*] Предзагрузка дефолтной сессии: {e}")
    else:
        print("[*] Сервер запущен в режиме ожидания входа через мобильное приложение.")


# ==========================================
# 3. ДИНАМИЧЕСКИЙ ВХОД (LOGIN)
# ==========================================
@app.api_route("/login", methods=["GET", "POST"])
@app.api_route("/v2/login", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/login", methods=["GET", "POST"])
@app.api_route("/api/v2/login", methods=["GET", "POST"])
async def dynamic_login(request: Request):
    username = None
    password = None

    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body_json = await request.json()
            username = body_json.get("username") or body_json.get("login") or body_json.get("Username")
            password = body_json.get("password") or body_json.get("Password")
        else:
            form_data = await request.form()
            username = form_data.get("username") or form_data.get("login") or form_data.get("Username")
            password = form_data.get("password") or form_data.get("Password")
    except Exception:
        pass

    if not username:
        username = request.query_params.get("username") or request.query_params.get("login")
        password = request.query_params.get("password")

    print(f"[Proxy] Попытка входа с телефона: username={username}")

    if not username or not password:
        return JSONResponse(status_code=400, content={"error": "Укажите логин и пароль"})

    try:
        new_backend = ESchoolsBackend(username, password)
        new_backend.login()

        session_token = str(uuid.uuid4())
        user_sessions[session_token] = new_backend

        response_data = {
            "token": session_token,
            "key": session_token,
            "user_id": 1,
            "status": "ok"
        }
        res = JSONResponse(content=response_data, status_code=200)
        res.set_cookie(key="session_token", value=session_token)
        return res
    except Exception as e:
        print(f"[-] Ошибка входа для {username}: {e}")
        return JSONResponse(status_code=401, content={"error": "Неверный логин или пароль"})


# ==========================================
# 4. ДАННЫЕ ДНЕВНИКА
# ==========================================

@app.api_route("/v2/user/current", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/user/current", methods=["GET", "POST"])
async def get_current_user(request: Request):
    try:
        b = resolve_backend(request)
    except Exception:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    return {
        "id": 1,
        "username": b.username,
        "first_name": b.first_name,
        "last_name": b.last_name,
        "father_name": b.father_name,
        "short_info": f"{b.last_name} {b.first_name}".strip(),
        "class_id": 1,
        "class_name": b.class_name,
        "subdomain": "brest16",
        "type": 1,
        "sex": 1,
        "email": ""
    }


@app.api_route("/v2/parent/pupils", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/parent/pupils", methods=["GET", "POST"])
async def get_parent_pupils(request: Request):
    try:
        b = resolve_backend(request)
    except Exception:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    return [
        {
            "id": 1,
            "first_name": b.first_name,
            "last_name": b.last_name,
            "father_name": b.father_name,
            "class_id": 1,
            "class_name": b.class_name,
            "type": 1
        }
    ]


@app.api_route("/v2/quarters/list", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/quarters/list", methods=["GET", "POST"])
@app.api_route("/v2/quarter/current", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/quarter/current", methods=["GET", "POST"])
async def get_quarters():
    return [
        {"id": 1, "number": 1, "roman_name": "I", "begin_date": "2026-09-01", "end_date": "2026-10-31", "is_holidays": False},
        {"id": 2, "number": 2, "roman_name": "II", "begin_date": "2026-11-09", "end_date": "2026-12-24", "is_holidays": False},
        {"id": 3, "number": 3, "roman_name": "III", "begin_date": "2027-01-11", "end_date": "2027-03-20", "is_holidays": False},
        {"id": 4, "number": 4, "roman_name": "IV", "begin_date": "2027-03-29", "end_date": "2027-05-31", "is_holidays": False}
    ]


@app.api_route("/v2/pupil/{pupil_id}/daybook/week/{week_param:path}", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/pupil/{pupil_id}/daybook/week/{week_param:path}", methods=["GET", "POST"])
@app.api_route("/v2/pupil/{pupil_id}/dnevnik/{week_param:path}", methods=["GET", "POST"])
async def get_daybook_week(request: Request, pupil_id: str, week_param: str):
    try:
        b = resolve_backend(request)
    except Exception:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    weeks = b.fetch_weeks()
    now_ms = int(time.time() * 1000)

    target_week_uuid = weeks[0]["uuid"] if weeks else "3d320ebf-f610-4e28-8278-089bfe461187"
    for w in weeks:
        if w["start_ts"] <= now_ms <= w["end_ts"]:
            target_week_uuid = w["uuid"]
            break

    new_days = b.fetch_new_lessons(target_week_uuid)

    old_format_days = []
    lesson_counter = 1

    for day in new_days:
        ts = day.get("date", 0) / 1000
        dt = datetime.fromtimestamp(ts, tz=MINSK_TZ)
        date_str = dt.strftime("%Y-%m-%d")

        day_lessons = []
        for slot in day.get("slots", []):
            mark_obj = slot.get("lesson_mark")
            mark_val = mark_obj.get("mark", "") if mark_obj else ""

            old_lesson = {
                "id": lesson_counter,
                "date": date_str,
                "number": slot.get("number", 1),
                "subject": slot.get("subject_title", "Урок"),
                "subject_short": (slot.get("subject_title") or "Урок")[:15],
                "theme": slot.get("topic") or "",
                "hometask": slot.get("homework") or "",
                "mark": mark_val,
                "mark_note": "",
                "cabinet": "",
                "teacher": "",
                "begin_time": (slot.get("start_time") or "08:00")[:5],
                "end_time": "",
                "attachments": [],
                "attachment_count": 0,
                "is_manual": False
            }
            day_lessons.append(old_lesson)
            lesson_counter += 1

        old_format_days.append({
            "date": date_str,
            "lessons": day_lessons,
            "holidays": False,
            "last_lesson_end_time": "15:00"
        })

    return old_format_days


@app.api_route("/v2/pupil/{pupil_id}/daybook/last-page", methods=["GET", "POST"])
@app.api_route("/subdomain-api/v2/pupil/{pupil_id}/daybook/last-page", methods=["GET", "POST"])
async def get_last_page(pupil_id: str):
    return {"status": "ok", "page": 1}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(request: Request, path: str):
    print(f"\n[⚠️ ЛОВУШКА] Неизвестный запрос: {request.method} /{path}")
    body = await request.body()
    if body:
        print(f"    Тело: {body.decode('utf-8', errors='ignore')}")
    return JSONResponse(content={"status": "ok", "data": []}, status_code=200)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("proxy_server:app", host="0.0.0.0", port=port, reload=False)
