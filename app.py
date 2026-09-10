# ==========================================
# 메가스터디 구내식당 메뉴 봇 (디스호스트 호스팅용)
# 사양: RAM 128MB / CPU 25% / 디스크 512MB 환경에 최적화
#
# 블로그 업로드 패턴: 보통 금요일에 "다음 주" 식단표가 올라옴 (가끔 목요일에 하루 일찍)
#  → 제목의 날짜 범위 시작일(예: "2026.07.20~07.24" → 07.20)의 ISO 주차로 매핑
#  → 업로드 요일(목/금/토)에 흔들리지 않음. 제목에 날짜가 없으면 게시일+3일로 폴백
#
# 명령어:
#  /오점뭐   - 이번 주 식단표에서 오늘(또는 선택한 요일) 점심만 크롭해서 표시
#              주말엔 다음 주 식단표(올라와 있으면)에서 월요일/선택 요일을 미리 보여줌
#  /이번주   - 이번 주(저번 금요일에 올라온) 식단표 전체 이미지
#  /다음주   - 다음 주 식단표 전체 이미지 (금요일 업로드 후부터 조회 가능)
#  /알림     - [서버 관리자 전용] 평일 지정 시각에 오늘 점심 자동 전송 (켜기/끄기/상태/휴무)
#  /건의     - GitHub 저장소 링크 안내 (Issue/PR 로 건의)
#  /디버그   - [서버 오너 전용] 크롤링 정보 + 월~금 크롭 결과 확인 (이번 주/다음 주)
#
# 자동 동작:
#  - 1시간마다 이번 주(목~일엔 다음 주까지) 식단표를 미리 캐싱 → 첫 호출자도 즉시 응답
#  - 크롤링 실패는 5분간 기억 → 글이 아직 없을 때 호출이 몰려도 블로그를 두드리지 않음
#  - 공휴일(내장 목록 + /알림 휴무 로 등록한 날)엔 점심 알림을 보내지 않음
# ==========================================

import asyncio
import datetime
import gc
import io
import json
import logging
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import discord
import requests
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageFilter

# ==========================================
# [설정]
# ==========================================
BLOG_ID = 'megafs01'
CATEGORY_NO = '41'
TITLE_KEYWORD = '[메가스터디 구내식당]'
GITHUB_URL = 'https://github.com/fdrn9999/mega-menu'
KST = ZoneInfo("Asia/Seoul")
REQUEST_TIMEOUT = 10          # 초
# 디스코드 무료 서버 업로드 한도(10MB)보다 약간 작게 — "캐시는 됐는데 전송만 실패"하는 사태 방지
MAX_DOWNLOAD_BYTES = int(9.5 * 1024 * 1024)
# 디코딩 후 비트맵 픽셀 상한 — 파일이 작아도 픽셀 수가 크면 RAM 폭탄 (2400만 px ≈ RGB 72MB)
MAX_IMAGE_PIXELS = 24_000_000
# 요일 크롭 확대 목표 폭 — 작은 이미지는 디스코드가 원본 크기 그대로 작게 표시하므로
# 임베드 폭 이상으로 키워서 꽉 차게 + 고해상도 디스플레이에서도 또렷하게
DAY_IMAGE_TARGET_WIDTH = 800
# 크롭/저장 방식이 바뀌면 올려서 기존 캐시를 자동 재생성
CACHE_VERSION = 3
# 자동 알림 기본 시각(KST)과 허용 범위(분) — 루프 지연/재시작으로 정각을 놓쳐도 범위 안이면 전송
DEFAULT_NOTIFY_TIME = "11:30"
NOTIFY_WINDOW_MIN = 10
# 크롤링 실패 쿨다운(초) — 글이 아직 없을 때 호출 폭주로 블로그를 계속 두드리지 않게
FAIL_COOLDOWN_SEC = 300

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
# 서버별 알림 설정 — git 에 없는 파일이라 디스호스트 자동 업데이트에도 유지됨 (.env 와 동일)
NOTIFY_PATH = os.path.join(BASE_DIR, "notify.json")

WEEKDAY_NAMES = ['월요일', '화요일', '수요일', '목요일', '금요일']

MSG_THIS_WEEK_MISSING = "이번 주 식단표를 블로그에서 찾을 수 없습니다."
MSG_NEXT_WEEK_MISSING = (
    "다음 주 식단표가 아직 올라오지 않았습니다.\n보통 **금요일 오전**에 블로그에 올라와요! 🕐"
)

# 구내식당 휴무일(공휴일) — 이 날엔 점심 알림을 보내지 않음
# 매년 같은 날짜인 공휴일 (월, 일). 주말과 겹쳐도 알림은 평일에만 돌므로 대체공휴일만 따로 관리
FIXED_HOLIDAYS = {
    (1, 1),    # 신정
    (3, 1),    # 삼일절
    (5, 5),    # 어린이날
    (6, 6),    # 현충일
    (8, 15),   # 광복절
    (10, 3),   # 개천절
    (10, 9),   # 한글날
    (12, 25),  # 성탄절
    # 제헌절(7/17)은 공휴일 재지정이 확정되면 추가
}
# 음력 기반 공휴일 + 대체공휴일 (해마다 바뀌므로 연 단위로 갱신 — 빠진 날은 /알림 휴무 로 등록)
LUNAR_AND_SUBSTITUTE_HOLIDAYS = {
    # 2026
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설날 연휴
    "2026-03-02",                              # 삼일절 대체공휴일 (3/1 일요일)
    "2026-05-24", "2026-05-25",                # 부처님오신날 + 대체공휴일 (5/24 일요일)
    "2026-06-03",                              # 지방선거
    "2026-08-17",                              # 광복절 대체공휴일 (8/15 토요일)
    "2026-09-24", "2026-09-25", "2026-09-26",  # 추석 연휴 (토요일 겹침은 대체공휴일 없음 — 일요일 겹침만 대체)
    "2026-10-05",                              # 개천절 대체공휴일 (10/3 토요일)
    # 2027 이후는 연말에 추가 (그 전까지는 /알림 휴무 로 서버별 등록)
}


def is_holiday(d, extra=()):
    """공휴일 여부: 고정 공휴일 + 음력/대체공휴일 목록 + 서버별 수동 등록 날짜"""
    iso = d.isoformat()
    return (
        (d.month, d.day) in FIXED_HOLIDAYS
        or iso in LUNAR_AND_SUBSTITUTE_HOLIDAYS
        or iso in extra
    )


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mega-menu")


def _load_dotenv():
    """같은 폴더의 .env 파일을 읽어 환경변수로 등록 (python-dotenv 의존성 없이 동작)"""
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:  # 실제 환경변수가 .env 보다 우선
                os.environ[key] = value


def load_token() -> str:
    """토큰 우선순위: 환경변수 DISCORD_TOKEN > .env 파일 > token.txt 파일"""
    _load_dotenv()

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if token:
        return token

    token_path = os.path.join(BASE_DIR, "token.txt")
    if os.path.exists(token_path):
        with open(token_path, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token

    log.error("토큰이 없습니다. .env 파일에 DISCORD_TOKEN=토큰 을 쓰거나, 환경변수 DISCORD_TOKEN 을 설정해주세요.")
    sys.exit(1)


# 권한 설정 — 슬래시 명령어만 쓰므로 기본 인텐트면 충분
intents = discord.Intents.default()


class MenuBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        log.info("슬래시 명령어 %d개 동기화 완료", len(synced))
        minute_tick.start()  # 점심 알림 + 프리페치 루프


bot = MenuBot(
    command_prefix="!",
    intents=intents,
    max_messages=None,                                # 메시지 캐시 끄기 (RAM 절약)
    chunk_guilds_at_startup=False,                    # 시작 시 멤버 목록 안 받음
    member_cache_flags=discord.MemberCacheFlags.none(),  # 멤버 캐시 끄기
)


# --------------------------------------------------------
# 주차 계산 / 캐시 파일 경로 헬퍼
# --------------------------------------------------------

def make_key(iso_year, iso_week):
    """예: 2026-W23"""
    return f"{iso_year}-W{iso_week:02d}"


def this_week_target(now):
    """이번 주 (ISO 연도, 주차)"""
    iso_year, iso_week, _ = now.isocalendar()
    return iso_year, iso_week


def next_week_target(now):
    """다음 주 (ISO 연도, 주차) — 연말/연초 경계도 안전"""
    iso_year, iso_week, _ = (now + datetime.timedelta(days=7)).isocalendar()
    return iso_year, iso_week


def week_monday(iso_year, iso_week):
    """해당 ISO 주차의 월요일 날짜(date)"""
    return datetime.date.fromisocalendar(iso_year, iso_week, 1)


def meta_path(key):
    return os.path.join(CACHE_DIR, f"menu_{key}_meta.json")


def full_image_path(key, ext):
    return os.path.join(CACHE_DIR, f"menu_{key}_full.{ext}")


def day_image_path(key, weekday):
    return os.path.join(CACHE_DIR, f"menu_{key}_day{weekday}.png")


# --------------------------------------------------------
# 이미지 크롭 (주차당 1번만 실행됨)
# --------------------------------------------------------

def crop_and_save_all_days(image_bytes, key):
    """
    원본 이미지를 한 번만 열어서 월~금 5개 컬럼을 크롭해 디스크에 저장.
    크롭 원본은 폭이 좁아 디스코드에서 작게 표시되므로,
    목표 폭까지 LANCZOS 확대 + 샤프닝해서 글씨가 잘 보이게 저장.
    저장 후 원본 비트맵은 즉시 해제 (128MB RAM 보호).
    """
    saved = 0
    img = Image.open(io.BytesIO(image_bytes))
    try:
        width, height = img.size

        # 파일 크기와 무관하게 픽셀 수가 크면 디코딩 시 RAM 폭탄 → 디코딩 전에 차단
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"이미지 픽셀 수 초과: {width}x{height}")

        # 크롭 좌표 기준 (식단표 레이아웃에 맞춰 튜닝한 값)
        top = int(height * 0.232)
        crop_height = int(height * 0.25)
        bottom = top + crop_height
        left_start = int(width * 0.169)
        content_width = int(width * 0.81)
        column_width = content_width / 5

        for weekday in range(5):
            try:
                left = left_start + int(column_width * weekday)
                right = left + int(column_width)
                cropped = img.crop((left, top, right, bottom))

                # 디스코드 임베드 폭에 맞춰 확대 (글씨 가독성)
                # 슈퍼샘플링: 목표의 2배로 확대 → 강하게 샤프닝 → 목표 크기로 축소
                # 한 번에 확대+샤프닝하는 것보다 글씨 경계의 번짐/할로가 적음
                if cropped.width < DAY_IMAGE_TARGET_WIDTH:
                    ss_width = DAY_IMAGE_TARGET_WIDTH * 2
                    scale = ss_width / cropped.width
                    big = cropped.resize(
                        (ss_width, int(cropped.height * scale)),
                        Image.LANCZOS,
                    )
                    cropped.close()
                    sharpened = big.filter(
                        ImageFilter.UnsharpMask(radius=4, percent=140, threshold=2)
                    )
                    big.close()
                    final = sharpened.resize(
                        (DAY_IMAGE_TARGET_WIDTH, sharpened.height // 2),
                        Image.LANCZOS,
                    )
                    sharpened.close()
                    # 축소 후 가벼운 마무리 샤프닝
                    cropped = final.filter(
                        ImageFilter.UnsharpMask(radius=1, percent=60, threshold=2)
                    )
                    final.close()

                cropped.save(day_image_path(key, weekday), format='PNG')
                cropped.close()
                saved += 1
            except Exception as e:
                log.warning("크롭 실패 (요일 %s): %s", weekday, e)
    finally:
        img.close()

    return saved


# --------------------------------------------------------
# 블로그 크롤링 (동기 함수 — asyncio.to_thread 로 실행됨)
# --------------------------------------------------------

def extract_high_quality_image_url(raw_url):
    if not raw_url:
        return None

    base_url = raw_url.split('?')[0]

    if 'postfiles.pstatic.net' in base_url or 'blogfiles.pstatic.net' in base_url:
        # 네이버 CDN 은 ?type=wN 으로 "폭 N px 리사이즈" 변형을 제공 (원본보다 크면 원본 크기로 캡)
        # 실측: w3840/w2000 → 원본 그대로, w966 → 966px 축소판,
        #       type 없는 base URL 은 원본이 아니라 100px 썸네일을 반환함 → 최후 폴백으로만
        return [
            f"{base_url}?type=w3840",
            f"{base_url}?type=w2000",
            f"{base_url}?type=w966",
            base_url,
        ]

    return [base_url]


def _post_target_week(clean_title, post_date):
    """
    이 게시물이 '몇 주차 식단표'인지 (ISO 연도, 주차)로 반환.

    제목의 날짜 범위 시작일(예: '2026.07.20~07.24' → 07.20)을 최우선으로 사용한다.
    이렇게 하면 업로드 요일에 흔들리지 않는다:
      - 예전엔 게시일+3일로만 매핑 → '금요일 업로드' 가정에 묶여 있었음
      - 목요일에 하루 일찍 올라오면 목+3=일요일이라 아직 같은 주 → 다음 주로 못 넘어가
        해당 주차 조회가 통째로 실패했음 (제목엔 올바른 주가 적혀 있는데도)
    제목에서 날짜를 못 읽을 때만 기존 게시일+3일 방식으로 폴백.
    """
    m = re.search(r'(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})', clean_title)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            iso_year, iso_week, _ = datetime.date(y, mo, d).isocalendar()
            return iso_year, iso_week
        except ValueError:
            pass  # 제목의 날짜가 비정상 → 폴백

    effective_date = post_date + datetime.timedelta(days=3)
    iso_year, iso_week, _ = effective_date.isocalendar()
    return iso_year, iso_week


# "그 주차 게시물이 아직 없음" 을 뜻하는 에러 표식 — 호출자마다 다른 안내 문구를 붙일 수 있게 문구 대신 표식으로 돌려줌
NOT_FOUND = "__not_found__"


def _fetch_menu_sync(target_year, target_week):
    """
    target_year/target_week 주차에 해당하는 식단표 게시물을 찾아
    메타데이터 + 이미지 URL 목록 반환. (data, error) 형태이며
    해당 주차 글이 없으면 error 로 NOT_FOUND 표식을 돌려준다.
    주차 판정은 _post_target_week (제목의 날짜 범위 우선) 기준.
    """
    list_url = f"https://m.blog.naver.com/api/blogs/{BLOG_ID}/post-list?categoryNo={CATEGORY_NO}&itemCount=5"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': f'https://m.blog.naver.com/{BLOG_ID}'
    }

    try:
        response = requests.get(list_url, headers=headers, timeout=REQUEST_TIMEOUT).json()
        items = response.get('result', {}).get('items', [])
        if not items:
            return None, "블로그 글 목록을 불러올 수 없습니다."

        target_post = None
        post_date = None

        for post in items:
            raw_title = post.get('titleWithInspectMessage', '')
            clean_title = BeautifulSoup(raw_title, "html.parser").get_text()

            if TITLE_KEYWORD not in clean_title:
                continue

            post_date = datetime.datetime.fromtimestamp(post['addDate'] / 1000, KST)
            post_iso_year, post_iso_week = _post_target_week(clean_title, post_date)

            if post_iso_year == target_year and post_iso_week == target_week:
                target_post = post
                break

        if not target_post:
            return None, NOT_FOUND

        log_no = target_post['logNo']
        post_view_url = f"https://blog.naver.com/PostView.naver?blogId={BLOG_ID}&logNo={log_no}"

        res = requests.get(post_view_url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(res.text, 'html.parser')

        main_content = soup.select_one(".se-main-container")
        if not main_content:
            main_content = soup.select_one("#postViewArea")
        if not main_content:
            main_content = soup.select_one(".se-component-content")

        image_urls = []

        if main_content:
            images = main_content.select("img")

            for img in images:
                raw_src = img.get('data-src') or img.get('src')

                if not raw_src:
                    continue

                if "postfiles.pstatic.net" in raw_src or "blogfiles.pstatic.net" in raw_src:
                    if any(x in raw_src for x in ["sticker", "profile", "emoticon", "lork", "icon"]):
                        continue

                    quality_urls = extract_high_quality_image_url(raw_src)
                    image_urls.extend(quality_urls)
                    break

        # 파싱 끝났으면 즉시 해제
        soup.decompose()

        if not image_urls:
            return None, "식단표 이미지를 본문에서 찾을 수 없습니다."

        return {
            "title": BeautifulSoup(target_post['titleWithInspectMessage'], "html.parser").get_text(),
            "date": post_date.strftime('%Y-%m-%d'),
            "week_num": target_week,
            "post_url": post_view_url,
            "image_urls": image_urls
        }, None

    except Exception as e:
        log.exception("블로그 크롤링 실패")
        return None, f"데이터 처리 중 오류가 발생했습니다: {str(e)}"


def _probe_size(url, headers):
    """본문을 받지 않고 Content-Length 헤더로 이미지 크기만 확인"""
    try:
        with requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT) as r:
            if r.status_code == 200:
                return int(r.headers.get('Content-Length') or 0)
    except Exception:
        pass
    return -1


def _download_capped(url, headers):
    """상한(MAX_DOWNLOAD_BYTES, 9.5MB)을 넘으면 중단하는 안전한 다운로드"""
    try:
        with requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT) as r:
            if r.status_code != 200:
                return None
            chunks = []
            total = 0
            for chunk in r.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    log.warning("다운로드 상한 초과로 중단: %s", url)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception:
        return None


def _download_best_image_sync(image_urls, post_url):
    """
    후보 URL들의 크기를 헤더로만 비교한 뒤, 가장 큰(=고화질) 1개만 실제 다운로드.
    """
    img_headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': post_url,
    }

    # 1) 크기만 먼저 확인
    candidates = []
    for url in image_urls:
        size = _probe_size(url, img_headers)
        if size > 0:
            candidates.append((size, url))

    # 2) 가장 큰 것부터 시도 (다운로드/업로드 상한 초과 후보는 건너뜀)
    for size, url in sorted(candidates, reverse=True):
        if size > MAX_DOWNLOAD_BYTES:
            continue
        content = _download_capped(url, img_headers)
        if content:
            return content, len(content), url

    # 3) Content-Length 를 안 주는 경우 폴백: 순서대로(원본 URL 우선) 받아서 첫 성공 사용
    for url in image_urls:
        content = _download_capped(url, img_headers)
        if content:
            return content, len(content), url

    return None, 0, None


# --------------------------------------------------------
# 주차 단위 디스크 캐시
#  - 무거운 작업은 주차당 1번, 이후엔 디스크의 작은 파일만 읽음
#  - 이번 주 + 다음 주 캐시만 유지, 지난 주차는 자동 삭제 (디스크 512MB 보호)
# --------------------------------------------------------

_cache_lock = asyncio.Lock()
_meta_memo = {}  # {주차키: 메타데이터 dict} — 작은 dict만 RAM에 유지
_fail_memo = {}  # {주차키: (monotonic 시각, 에러 메시지)} — 실패 직후 재크롤링 방지


def _valid_keys(now):
    """지금 시점에 유지해야 할 캐시 주차키 (이번 주 + 다음 주)"""
    return {
        make_key(*this_week_target(now)),
        make_key(*next_week_target(now)),
    }


def _cleanup_old_cache(valid_keys):
    """유효 주차가 아닌 캐시 파일 전부 삭제"""
    if not os.path.isdir(CACHE_DIR):
        return
    for name in os.listdir(CACHE_DIR):
        if name.startswith("menu_") and not any(f"menu_{k}_" in name for k in valid_keys):
            try:
                os.remove(os.path.join(CACHE_DIR, name))
            except OSError:
                pass


def _build_week_cache_sync(key, target_year, target_week, valid_keys):
    """
    크롤링 → 다운로드 → 크롭 → 디스크 저장. (meta, error) 반환.
    error 는 NOT_FOUND 표식이거나 사용자에게 보여줄 오류 문구.
    """
    data, error_msg = _fetch_menu_sync(target_year, target_week)
    if error_msg:
        return None, error_msg

    # 여기서 예외가 새어 나가면 유저는 "생각 중..."에서 영원히 멈춤 → 전부 잡아서 메시지로 변환
    try:
        image, size, final_url = _download_best_image_sync(data['image_urls'], data['post_url'])
        if not image:
            return None, f"모든 이미지 URL에서 다운로드 실패\n\n직접 확인: {data['post_url']}"

        # 확장자 결정
        ext = "jpg"
        if final_url and "." in final_url:
            ext = final_url.split(".")[-1].split("?")[0].lower()
            if len(ext) > 4 or not ext.isalnum():
                ext = "jpg"

        os.makedirs(CACHE_DIR, exist_ok=True)
        _cleanup_old_cache(valid_keys)

        # 원본 저장 (전체 메뉴표용)
        with open(full_image_path(key, ext), "wb") as f:
            f.write(image)

        # 월~금 크롭 저장
        saved = crop_and_save_all_days(image, key)

        meta = {
            **data,
            "file_size": size,
            "final_url": final_url,
            "ext": ext,
            "cropped_days": saved,
            "cache_ver": CACHE_VERSION,
        }

        with open(meta_path(key), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        # 원본 바이트 즉시 해제
        del image
        gc.collect()

        log.info("식단표 캐시 생성: %s (원본 %.1f KB, 크롭 %d개)", key, size / 1024, saved)
        return meta, None

    except Exception:
        log.exception("캐시 생성 실패: %s", key)
        return None, f"식단표 처리 중 오류가 발생했습니다.\n\n직접 확인: {data['post_url']}"


def invalidate_week_cache(key):
    """
    RAM 메모에서 해당 주차를 지워 다음 호출 때 디스크 검증(_load_meta_sync)부터 다시 타게 한다.
    캐시 생성 후 이미지 파일이 사라진 경우(수동 삭제 등)에 호출 — 그렇지 않으면 메모가 살아 있는 동안
    그 주 내내 '이미지 없음' 만 뜬다.
    """
    if _meta_memo.pop(key, None) is not None:
        log.warning("캐시 파일 누락 → RAM 메모 무효화: %s", key)


def _load_meta_sync(key):
    """
    디스크에서 메타데이터 읽기. 없거나, 구버전이거나, 메타가 가리키는 이미지 파일이
    실제로 없으면(디스크 정리/쓰기 중단 등) None 을 돌려줘 재생성되게 한다.
    → 예전엔 메타만 믿어서 파일이 사라지면 그 주 내내 '크롭 실패'만 떴음.
    """
    path = meta_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return None

    if meta.get("cache_ver") != CACHE_VERSION:
        return None  # 구버전 캐시 → 새 방식으로 재생성

    if not os.path.exists(full_image_path(key, meta.get("ext", "jpg"))):
        log.warning("캐시 메타는 있는데 원본 이미지가 없음 → 재생성: %s", key)
        return None
    day_files = sum(os.path.exists(day_image_path(key, wd)) for wd in range(5))
    if day_files != meta.get("cropped_days", 5):
        log.warning("캐시 요일 이미지 수 불일치(%d≠%s) → 재생성: %s", day_files, meta.get("cropped_days"), key)
        return None
    return meta


async def ensure_week_cache(target_year, target_week, not_found_msg):
    """
    해당 주차 캐시를 보장. (key, meta, error_msg, not_found) 반환.
      - 성공: (key, meta, None, False)
      - 그 주차 글이 아직 없음: (key, None, not_found_msg, True)
      - 네트워크/처리 오류 등 일시 실패: (key, None, 오류문구, False)
    이미 캐시가 있으면 네트워크/CPU 작업 없이 즉시 반환.
    실패 쿨다운은 '글 없음' 여부만 기억하므로, 쿨다운 중에도 호출자별 안내 문구가 유지된다.
    """
    key = make_key(target_year, target_week)
    now = datetime.datetime.now(KST)
    valid = _valid_keys(now)

    # RAM 메모에 있으면 바로 반환
    if key in _meta_memo:
        return key, _meta_memo[key], None, False

    async with _cache_lock:
        # 락 대기 중 다른 요청이 만들었을 수 있으니 재확인
        if key in _meta_memo:
            return key, _meta_memo[key], None, False

        # 방금 실패한 주차면 쿨다운 동안 재크롤링 없이 즉시 응답 (블로그/CPU 보호)
        failed = _fail_memo.get(key)
        if failed and time.monotonic() - failed[0] < FAIL_COOLDOWN_SEC:
            _, error_msg, not_found = failed
            return key, None, (not_found_msg if not_found else error_msg), not_found

        meta = await asyncio.to_thread(_load_meta_sync, key)
        if meta is None:
            meta, error_msg = await asyncio.to_thread(
                _build_week_cache_sync, key, target_year, target_week, valid
            )
            if error_msg:
                not_found = error_msg == NOT_FOUND
                _fail_memo[key] = (time.monotonic(), error_msg, not_found)
                return key, None, (not_found_msg if not_found else error_msg), not_found
            _fail_memo.pop(key, None)

        # 지난 주차 메모 정리
        for old in [k for k in _meta_memo if k not in valid]:
            del _meta_memo[old]
        for old in [k for k in _fail_memo if k not in valid]:
            del _fail_memo[old]

        _meta_memo[key] = meta
        return key, meta, None, False


# --------------------------------------------------------
# 서버별 점심 알림 설정 (notify.json)
#  형식: {"길드ID": {"channel_id": int, "time": "HH:MM", "last_sent": "YYYY-MM-DD",
#                    "role_id": int(선택), "skip_dates": ["YYYY-MM-DD", ...](선택)}}
# --------------------------------------------------------

def _load_notify_conf():
    if not os.path.exists(NOTIFY_PATH):
        return {}
    try:
        with open(NOTIFY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        log.warning("notify.json 읽기 실패 — 알림 설정을 비운 채 시작")
        return {}


def save_notify_conf():
    """임시 파일에 쓰고 교체 — 쓰는 도중 봇이 죽어도 설정 파일이 깨지지 않음"""
    tmp = NOTIFY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_notify_conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NOTIFY_PATH)


_notify_conf = _load_notify_conf()


def parse_hhmm(text):
    """'9:5' → '09:05', 잘못된 입력은 None"""
    try:
        hh, mm = text.strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return f"{hh:02d}:{mm:02d}"
    except (ValueError, AttributeError):
        pass
    return None


# --------------------------------------------------------
# 요일 점심 임베드 헬퍼 (/오점뭐, 자동 알림 공용)
# --------------------------------------------------------

def build_day_embed(key, meta, weekday, day_date, is_today, is_next_week=False):
    """요일 크롭 이미지 임베드 + 첨부 파일 생성. 크롭 파일이 없으면 (None, None)."""
    path = day_image_path(key, weekday)
    if not os.path.exists(path):
        return None, None

    day_name = WEEKDAY_NAMES[weekday]
    date_str = day_date.strftime('%Y-%m-%d')
    if is_today:
        title = f"🍚 오늘의 점심 메뉴 ({day_name})"
    elif is_next_week:
        title = f"🍚 다음 주 {day_name} 점심 메뉴 (미리보기)"
    else:
        title = f"🍚 {day_name} 점심 메뉴"

    embed = discord.Embed(
        title=title,
        description=f"**{date_str}** 메가스터디 구내식당",
        color=0x2ecc71,
        url=meta['post_url']
    )

    filename = f"lunch_menu_{date_str}.png"
    image_file = discord.File(path, filename=filename)

    embed.set_image(url=f"attachment://{filename}")
    embed.add_field(
        name="📎 전체 메뉴 보기",
        value=f"[블로그에서 보기]({meta['post_url']})",
        inline=False
    )
    full_cmd = "/다음주" if is_next_week else "/이번주"
    embed.set_footer(text=f"{meta['week_num']}주차 식단표 · 전체 메뉴는 {full_cmd}")
    return embed, image_file


# --------------------------------------------------------
# 전체 식단표 전송 헬퍼 (/이번주, /다음주 공용)
# --------------------------------------------------------

async def send_full_sheet(interaction, key, meta, title):
    path = full_image_path(key, meta['ext'])
    if not os.path.exists(path):
        invalidate_week_cache(key)
        await interaction.followup.send(
            f"❌ 캐시된 이미지가 없습니다.\n\n직접 확인: {meta['post_url']}"
        )
        return

    embed = discord.Embed(
        title=title,
        description=f"**{meta['week_num']}주차 식단표** (게시일: {meta['date']})",
        color=0x3498db,
        url=meta['post_url']
    )

    filename = f"weekly_menu_{meta['date']}.{meta['ext']}"
    image_file = discord.File(path, filename=filename)

    embed.set_image(url=f"attachment://{filename}")
    embed.add_field(
        name="📎 원본 링크",
        value=f"[블로그에서 보기]({meta['post_url']})",
        inline=False
    )
    embed.set_footer(text=f"이미지 크기: {meta['file_size'] / 1024:.1f} KB")

    try:
        await interaction.followup.send(embed=embed, file=image_file)
    except discord.HTTPException as e:
        # 서버 업로드 한도 초과 등 — 이미지 없이 링크라도 안내
        log.warning("전체 식단표 전송 실패 (%s): %s", key, e)
        await interaction.followup.send(
            f"⚠️ 이미지 전송에 실패했습니다. (파일이 서버 업로드 한도를 넘었을 수 있어요)\n\n"
            f"직접 확인: {meta['post_url']}"
        )


# --------------------------------------------------------
# 봇 이벤트 / 명령어
# --------------------------------------------------------

@bot.event
async def on_ready():
    log.info("✅ 로그인 성공: %s", bot.user)
    await bot.change_presence(activity=discord.Game(name="/오점뭐 — 오늘 점심 확인"))


@bot.tree.command(name="오점뭐", description="오늘 점심 메뉴를 보여줍니다. (요일을 고르면 그 요일 점심)")
@app_commands.describe(요일="다른 요일 점심이 궁금하면 선택 (기본: 오늘)")
@app_commands.choices(요일=[
    app_commands.Choice(name=name, value=i) for i, name in enumerate(WEEKDAY_NAMES)
])
async def today_lunch(interaction: discord.Interaction, 요일: app_commands.Choice[int] | None = None):
    now = datetime.datetime.now(KST)
    today = now.date()
    is_weekend = now.weekday() >= 5
    weekday = 요일.value if 요일 is not None else now.weekday()  # 0=월 ~ 6=일

    await interaction.response.defer()

    # 평일: 이번 주 식단 (저번 주 금요일에 올라온 게시물 — 금요일에 조회해도 여전히 이번 주 것)
    # 주말: '이번 주'는 이미 지나간 주라 지난 메뉴가 나옴 → 다음 주 식단표를 먼저 찾고,
    #       아직 안 올라왔으면 이번 주 것으로 폴백 (요일을 고른 경우에만)
    is_next_week = False
    if is_weekend:
        target_year, target_week = next_week_target(now)
        key, meta, error_msg, not_found = await ensure_week_cache(
            target_year, target_week, MSG_NEXT_WEEK_MISSING
        )
        if error_msg is None:
            is_next_week = True
            if 요일 is None:
                weekday = 0  # 주말 기본: 다음 주 월요일 미리보기
        elif 요일 is None:
            # 다음 주 식단표가 아직 없음 → 쉬는 날 안내
            if not_found:
                reason = "다음 주 식단표는 아직 안 올라왔어요. (보통 **금요일 오전**에 올라와요)"
            else:
                reason = f"⚠️ {error_msg}"
            await interaction.followup.send(
                "🛌 주말에는 구내식당이 쉬어요.\n"
                f"{reason}\n"
                "이번 주 메뉴는 `/이번주`, 이번 주 특정 요일 점심은 `/오점뭐 요일:` 로 확인하세요!"
            )
            return
        else:
            target_year, target_week = this_week_target(now)
            key, meta, error_msg, not_found = await ensure_week_cache(
                target_year, target_week, MSG_THIS_WEEK_MISSING
            )
    else:
        target_year, target_week = this_week_target(now)
        day_date_hint = week_monday(target_year, target_week) + datetime.timedelta(days=weekday)
        key, meta, error_msg, not_found = await ensure_week_cache(
            target_year, target_week,
            f"{day_date_hint.isoformat()}에 해당하는 식단표가 블로그에 없습니다."
        )

    if error_msg:
        await interaction.followup.send(f"⚠️ **{error_msg}**")
        return

    day_date = week_monday(target_year, target_week) + datetime.timedelta(days=weekday)

    try:
        embed, image_file = build_day_embed(
            key, meta, weekday, day_date,
            is_today=(day_date == today), is_next_week=is_next_week,
        )
        if embed is None:
            invalidate_week_cache(key)
            await interaction.followup.send(
                f"❌ 이미지 크롭 실패\n\n직접 확인: {meta['post_url']}"
            )
            return

        await interaction.followup.send(embed=embed, file=image_file)

    except Exception as e:
        log.exception("메뉴 처리 실패")
        await interaction.followup.send(
            f"❌ 이미지 처리 실패: {e}\n\n직접 확인: {meta['post_url']}"
        )


@bot.tree.command(name="이번주", description="이번 주 전체 식단표를 보여줍니다.")
async def this_week(interaction: discord.Interaction):
    await interaction.response.defer()

    now = datetime.datetime.now(KST)
    target_year, target_week = this_week_target(now)
    key, meta, error_msg, _ = await ensure_week_cache(
        target_year, target_week, MSG_THIS_WEEK_MISSING
    )

    if error_msg:
        await interaction.followup.send(f"⚠️ **{error_msg}**")
        return

    title = "📅 이번 주 전체 메뉴표"
    if now.weekday() >= 5:
        title += " (지나간 주 · 다음 주는 /다음주)"
    await send_full_sheet(interaction, key, meta, title)


@bot.tree.command(name="다음주", description="다음 주 전체 식단표를 보여줍니다. (매주 금요일 업로드 후 조회 가능)")
async def next_week(interaction: discord.Interaction):
    await interaction.response.defer()

    now = datetime.datetime.now(KST)
    target_year, target_week = next_week_target(now)
    key, meta, error_msg, _ = await ensure_week_cache(
        target_year, target_week, MSG_NEXT_WEEK_MISSING
    )

    if error_msg:
        await interaction.followup.send(f"⚠️ **{error_msg}**")
        return

    await send_full_sheet(interaction, key, meta, "📅 다음 주 전체 메뉴표")


@bot.tree.command(name="건의", description="봇에 대한 건의/버그 제보 방법을 알려줍니다.")
async def suggest(interaction: discord.Interaction):
    embed = discord.Embed(
        title="💡 건의는 GitHub에서 받아요!",
        description=(
            "이 봇은 오픈소스로 관리됩니다.\n\n"
            f"🐞 **버그 제보 / 아이디어** → [Issue 등록]({GITHUB_URL}/issues)\n"
            f"🔧 **직접 코드 수정** → [Pull Request]({GITHUB_URL}/pulls)\n\n"
            f"저장소: {GITHUB_URL}"
        ),
        color=0x7289da,
        url=GITHUB_URL,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------
# /알림 — 평일 점심 자동 알림 (서버 관리자 전용)
# --------------------------------------------------------

notify_group = app_commands.Group(
    name="알림",
    description="평일 점심 자동 알림 설정 (서버 관리자 전용)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


async def _check_manager(interaction):
    """서버 관리 권한 확인 — 없으면 안내 메시지까지 보내고 False 반환"""
    if not interaction.guild or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "⛔ **서버 관리** 권한이 있어야 사용할 수 있어요.", ephemeral=True
        )
        return False
    return True


@notify_group.command(name="켜기", description="평일 지정 시각에 오늘 점심 메뉴를 자동으로 올립니다.")
@app_commands.describe(
    채널="알림을 보낼 채널 (기본: 지금 이 채널)",
    시간=f"알림 시각, 24시간제 HH:MM (기본: {DEFAULT_NOTIFY_TIME})",
    멘션="알림 때 함께 호출할 역할 (선택)",
)
async def notify_on(
    interaction: discord.Interaction,
    채널: discord.TextChannel | None = None,
    시간: str = DEFAULT_NOTIFY_TIME,
    멘션: discord.Role | None = None,
):
    if not await _check_manager(interaction):
        return

    hhmm = parse_hhmm(시간)
    if hhmm is None:
        await interaction.response.send_message(
            f"⚠️ 시간은 24시간제 `HH:MM` 형식으로 입력해주세요. (예: `{DEFAULT_NOTIFY_TIME}`)",
            ephemeral=True,
        )
        return

    channel = 채널 or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "⚠️ 일반 텍스트 채널만 알림 채널로 쓸 수 있어요.", ephemeral=True
        )
        return

    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links and perms.attach_files):
        await interaction.response.send_message(
            f"⚠️ {channel.mention} 채널에 봇 권한이 부족해요.\n"
            "필요 권한: **메시지 보내기, 링크 첨부(임베드), 파일 첨부**",
            ephemeral=True,
        )
        return

    # 역할 멘션: '모두 멘션' 이 꺼진 역할(또는 @everyone)은 봇에 '모든 역할 멘션' 권한이 있어야 실제로 울림
    if 멘션 is not None and not 멘션.mentionable and not perms.mention_everyone:
        await interaction.response.send_message(
            f"⚠️ {멘션.mention} 역할은 '모두가 이 역할을 멘션할 수 있음' 이 꺼져 있어서\n"
            f"봇에 {channel.mention} 채널의 **@everyone, @here, 모든 역할 멘션** 권한이 있어야 호출돼요.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    prev = _notify_conf.get(str(interaction.guild_id), {})
    conf = {"channel_id": channel.id, "time": hhmm}
    if 멘션 is not None:
        conf["role_id"] = 멘션.id
    if prev.get("skip_dates"):
        conf["skip_dates"] = prev["skip_dates"]  # 등록해 둔 휴무일은 유지
    _notify_conf[str(interaction.guild_id)] = conf
    save_notify_conf()

    msg = f"✅ 평일 **{hhmm}** 에 {channel.mention} 채널로 오늘 점심 메뉴를 보내드릴게요!"
    if 멘션 is not None:
        msg += f"\n📣 알림마다 {멘션.mention} 역할을 함께 호출해요."
    msg += "\n🏖️ 공휴일에는 보내지 않아요. 휴무일 추가 등록은 `/알림 휴무`"
    await interaction.response.send_message(
        msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@notify_group.command(name="휴무", description="식당 휴무일을 등록/해제합니다. (그날은 알림을 보내지 않음)")
@app_commands.describe(날짜="YYYY-MM-DD 형식 (이미 등록된 날짜면 해제)")
async def notify_skip(interaction: discord.Interaction, 날짜: str):
    if not await _check_manager(interaction):
        return

    try:
        d = datetime.date.fromisoformat(날짜.strip())
    except ValueError:
        await interaction.response.send_message(
            "⚠️ 날짜는 `YYYY-MM-DD` 형식으로 입력해주세요. (예: `2026-12-31`)", ephemeral=True
        )
        return

    conf = _notify_conf.get(str(interaction.guild_id))
    if not conf:
        await interaction.response.send_message(
            "이 서버에는 알림이 꺼져 있어요. 먼저 `/알림 켜기` 로 켜주세요.", ephemeral=True
        )
        return

    iso = d.isoformat()
    skip = conf.setdefault("skip_dates", [])
    if iso in skip:
        skip.remove(iso)
        msg = f"✅ **{iso}** 휴무 등록을 해제했어요."
    elif is_holiday(d):
        await interaction.response.send_message(
            f"**{iso}** 은 이미 공휴일로 등록돼 있어서 알림이 가지 않아요.", ephemeral=True
        )
        return
    else:
        skip.append(iso)
        skip.sort()
        msg = f"✅ **{iso}** 을 휴무일로 등록했어요. 그날은 알림을 보내지 않아요."

    # 지난 날짜는 정리 (목록이 무한히 커지지 않게)
    today_iso = datetime.datetime.now(KST).date().isoformat()
    conf["skip_dates"] = [s for s in skip if s >= today_iso]
    if not conf["skip_dates"]:
        del conf["skip_dates"]
    save_notify_conf()
    await interaction.response.send_message(msg, ephemeral=True)


@notify_group.command(name="끄기", description="이 서버의 점심 자동 알림을 끕니다.")
async def notify_off(interaction: discord.Interaction):
    if not await _check_manager(interaction):
        return

    if _notify_conf.pop(str(interaction.guild_id), None) is None:
        await interaction.response.send_message("이 서버에는 켜져 있는 알림이 없어요.", ephemeral=True)
        return

    save_notify_conf()
    await interaction.response.send_message("🔕 점심 자동 알림을 껐어요.", ephemeral=True)


@notify_group.command(name="상태", description="이 서버의 점심 알림 설정을 확인합니다.")
async def notify_status(interaction: discord.Interaction):
    conf = _notify_conf.get(str(interaction.guild_id))
    if not conf:
        await interaction.response.send_message(
            "이 서버에는 알림이 꺼져 있어요. `/알림 켜기` 로 시작!", ephemeral=True
        )
        return

    msg = f"🔔 평일 **{conf.get('time', DEFAULT_NOTIFY_TIME)}** 에 <#{conf['channel_id']}> 채널로 알림 중"
    if conf.get("role_id"):
        msg += f"\n📣 함께 호출: <@&{conf['role_id']}>"
    if conf.get("skip_dates"):
        msg += "\n🏖️ 등록된 휴무일: " + ", ".join(conf["skip_dates"])
    msg += "\n(공휴일은 자동으로 건너뜁니다)"
    if conf.get("last_sent"):
        msg += f"\n마지막 전송: {conf['last_sent']}"
    await interaction.response.send_message(
        msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


bot.tree.add_command(notify_group)


@bot.tree.command(name="디버그", description="[서버 오너 전용] 이미지 URL과 월~금 크롭 결과를 모두 확인합니다.")
@app_commands.guild_only()
@app_commands.describe(주차="확인할 주차 (기본: 이번 주)")
@app_commands.choices(주차=[
    app_commands.Choice(name="이번 주", value="this"),
    app_commands.Choice(name="다음 주", value="next"),
])
async def debug(interaction: discord.Interaction, 주차: app_commands.Choice[str] | None = None):

    # DM 에서는 guild 가 None 이라 오너 체크가 통과돼 버림 → guild_only + 이중 확인
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("⛔ 이 명령어는 서버 오너만 사용 가능.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    now = datetime.datetime.now(KST)
    if 주차 is not None and 주차.value == "next":
        label = "다음 주"
        target_year, target_week = next_week_target(now)
        missing_msg = MSG_NEXT_WEEK_MISSING
    else:
        label = "이번 주"
        target_year, target_week = this_week_target(now)
        missing_msg = MSG_THIS_WEEK_MISSING
    key, meta, error_msg, _ = await ensure_week_cache(target_year, target_week, missing_msg)

    if error_msg:
        await interaction.followup.send(f"⚠️ {error_msg}", ephemeral=True)
        return

    debug_msg = f"""**디버그 정보** ({label}: {key})
- 게시물: {meta['title']}
- 날짜: {meta['date']}
- 블로그 링크: {meta['post_url']}
- 다운로드된 이미지: {meta['file_size'] / 1024:.1f} KB ({meta['final_url']})
- 크롭된 요일 수: {meta.get('cropped_days', '?')}/5

**시도할 이미지 URL들:**
"""

    for i, url in enumerate(meta['image_urls'], 1):
        debug_msg += f"\n{i}. ```{url}```"

    await interaction.followup.send(debug_msg, ephemeral=True)

    try:
        sent = 0
        for weekday in range(5):
            path = day_image_path(key, weekday)
            if not os.path.exists(path):
                continue

            embed = discord.Embed(
                title=f"📋 {WEEKDAY_NAMES[weekday]} 점심 메뉴",
                color=0xe74c3c
            )

            filename = f"debug_day{weekday}.png"
            image_file = discord.File(path, filename=filename)
            embed.set_image(url=f"attachment://{filename}")

            await interaction.followup.send(embed=embed, file=image_file, ephemeral=True)
            sent += 1

        if sent == 0:
            await interaction.followup.send("❌ 크롭된 이미지가 없습니다.", ephemeral=True)

    except Exception as e:
        log.exception("디버그 크롭 실패")
        await interaction.followup.send(f"❌ 크롭 처리 실패: {e}", ephemeral=True)


# --------------------------------------------------------
# 백그라운드 루프 — 평일 점심 알림 + 식단표 프리페치
# --------------------------------------------------------

_last_prefetch_hour = None  # (날짜, 시) — 프리페치는 시간당 1번만 시도


def _minute_of_day(dt):
    return dt.hour * 60 + dt.minute


async def _send_lunch_notification(guild_id, conf, window_end_min):
    """
    설정된 채널로 오늘 점심 크롭 이미지를 전송.
    반환값: True = 오늘 처리 끝(전송했거나 더 시도해도 소용없음), False = 일시 오류라 잠시 후 재시도.
      - 식단표 자체가 없음(NOT_FOUND): 그날은 한 번만 안내하고 끝
      - 네트워크/처리 오류: 알림 허용 시간(NOTIFY_WINDOW_MIN) 안에서 재시도, 마지막 시도에만 채널에 안내
      - 채널에 보낼 권한이 없음(Forbidden): 그 서버 알림을 자동으로 끔 (매일 조용히 실패하던 것 방지)
    window_end_min: 알림 허용 창이 끝나는 분(하루 기준). 크롤링이 오래 걸려 창을 넘겼는지는
    시도가 끝난 뒤의 시각으로 다시 판단한다 (루프 시작 시각만 보면 마지막 안내를 영영 못 보낼 수 있음).
    """
    channel = bot.get_channel(conf["channel_id"])
    if channel is None:
        log.warning("알림 채널을 찾을 수 없음 (guild %s, channel %s)", guild_id, conf["channel_id"])
        return True

    now = datetime.datetime.now(KST)
    weekday = now.weekday()
    target_year, target_week = this_week_target(now)
    key, meta, error_msg, not_found = await ensure_week_cache(
        target_year, target_week, MSG_THIS_WEEK_MISSING
    )

    try:
        if error_msg:
            # 창의 마지막 1분에 들어섰으면(시도 후 시각 기준) 더는 미루지 않고 안내 후 마감
            final_attempt = _minute_of_day(datetime.datetime.now(KST)) >= window_end_min - 1
            if not not_found and not final_attempt:
                log.info("점심 알림 일시 오류 → 재시도 예정 (guild %s): %s", guild_id, error_msg.replace("\n", " "))
                return False
            await channel.send(f"🔔 오늘의 점심 알림\n⚠️ **{error_msg}**")
            return True

        embed, image_file = build_day_embed(key, meta, weekday, now, is_today=True)
        if embed is None:
            invalidate_week_cache(key)
            await channel.send(f"🔔 오늘의 점심 알림\n❌ 이미지 크롭 실패\n\n직접 확인: {meta['post_url']}")
            return True

        content = "🔔 오늘의 점심 알림"
        if conf.get("role_id"):
            content += f" <@&{conf['role_id']}>"
        await channel.send(
            content=content, embed=embed, file=image_file,
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=True),
        )
        return True

    except discord.Forbidden:
        log.warning("알림 채널 권한 없음 → 알림 자동 해제 (guild %s, channel %s)", guild_id, conf["channel_id"])
        _notify_conf.pop(guild_id, None)
        return True


async def _prefetch_caches(now):
    """식단표를 미리 캐싱해 첫 호출자도 기다리지 않게 (캐시가 있으면 아무것도 안 함)"""
    target_year, target_week = this_week_target(now)
    _, _, err, _ = await ensure_week_cache(target_year, target_week, MSG_THIS_WEEK_MISSING)
    if err:
        log.info("프리페치(이번 주) 실패: %s", err.replace("\n", " "))

    # 목~일: 금요일(가끔 목요일)에 올라오는 다음 주 식단표도 미리 캐싱
    if now.weekday() >= 3:
        target_year, target_week = next_week_target(now)
        _, _, err, _ = await ensure_week_cache(target_year, target_week, MSG_NEXT_WEEK_MISSING)
        if err:
            log.debug("프리페치(다음 주) 실패: %s", err.replace("\n", " "))


@tasks.loop(minutes=1)
async def minute_tick():
    global _last_prefetch_hour
    try:
        now = datetime.datetime.now(KST)

        # ① 평일 점심 알림 — 루프 지연/재시작으로 정각을 놓쳐도 허용 범위 안이면 전송
        if now.weekday() < 5 and _notify_conf:
            today = now.date().isoformat()
            now_min = now.hour * 60 + now.minute
            changed = False
            for guild_id, conf in list(_notify_conf.items()):
                try:
                    hh, mm = conf.get("time", DEFAULT_NOTIFY_TIME).split(":")
                    target_min = int(hh) * 60 + int(mm)
                except ValueError:
                    continue
                elapsed = now_min - target_min
                if not (0 <= elapsed < NOTIFY_WINDOW_MIN):
                    continue
                if conf.get("last_sent") == today:
                    continue
                if is_holiday(now.date(), conf.get("skip_dates", ())):
                    conf["last_sent"] = today  # 휴무일 — 보내지 않고 오늘은 처리된 것으로 기록
                    changed = True
                    log.info("휴무일이라 점심 알림 건너뜀 (guild %s, %s)", guild_id, today)
                    continue
                window_end_min = target_min + NOTIFY_WINDOW_MIN
                try:
                    done = await _send_lunch_notification(guild_id, conf, window_end_min)
                except Exception:
                    log.exception("점심 알림 전송 실패 (guild %s)", guild_id)
                    # 예외도 창의 마지막 1분(시도 후 시각 기준)이면 포기하고 마감
                    done = _minute_of_day(datetime.datetime.now(KST)) >= window_end_min - 1
                if done:
                    conf["last_sent"] = today  # 전송(또는 포기) 후 기록 — 같은 날 도배 방지
                    changed = True
            if changed:
                save_notify_conf()

        # ② 시간당 1번: 식단표 프리페치
        hour_key = (now.date(), now.hour)
        if hour_key != _last_prefetch_hour:
            _last_prefetch_hour = hour_key
            await _prefetch_caches(now)

    except Exception:
        log.exception("백그라운드 루프 오류")


@minute_tick.before_loop
async def _wait_until_ready():
    await bot.wait_until_ready()


@bot.event
async def on_guild_remove(guild):
    """봇이 서버에서 제거되면 해당 서버의 알림 설정도 정리"""
    if _notify_conf.pop(str(guild.id), None) is not None:
        save_notify_conf()


# ==========================================
# 실행
# ==========================================
if __name__ == "__main__":
    bot.run(load_token())
