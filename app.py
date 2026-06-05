# ==========================================
# 메가스터디 구내식당 메뉴 봇 (디스호스트 호스팅용)
# 사양: RAM 128MB / CPU 25% / 디스크 512MB 환경에 최적화
#
# 블로그 업로드 패턴: 매주 금요일에 "다음 주" 식단표가 올라옴
#  → N주차 식단 = (N-1)주차 금요일 게시물
#  → 게시일 + 3일의 ISO 주차로 매핑 (금/토/일 게시 모두 다음 주로 매핑됨)
#
# 명령어:
#  /오점뭐   - 평일: 이번 주 식단표에서 오늘 요일 점심만 크롭해서 표시
#  /이번주   - 이번 주(저번 금요일에 올라온) 식단표 전체 이미지
#  /다음주   - 다음 주 식단표 전체 이미지 (금요일 업로드 후부터 조회 가능)
#  /건의     - GitHub 저장소 링크 안내 (Issue/PR 로 건의)
#  /디버그   - [서버 오너 전용] 크롤링 정보 + 월~금 크롭 결과 확인
# ==========================================

import asyncio
import datetime
import gc
import io
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

import discord
import requests
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")

WEEKDAY_NAMES = ['월요일', '화요일', '수요일', '목요일', '금요일']

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


def _fetch_menu_sync(target_year, target_week, not_found_msg):
    """
    target_year/target_week 주차에 해당하는 식단표 게시물을 찾아
    메타데이터 + 이미지 URL 목록 반환.
    (금요일에 올라온 글은 게시일+3일 보정으로 '다음 주' 식단으로 매핑됨)
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
            effective_date = post_date + datetime.timedelta(days=3)
            post_iso_year, post_iso_week, _ = effective_date.isocalendar()

            if post_iso_year == target_year and post_iso_week == target_week:
                target_post = post
                break

        if not target_post:
            return None, not_found_msg

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
    """상한(12MB)을 넘으면 중단하는 안전한 다운로드"""
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


def _build_week_cache_sync(key, target_year, target_week, not_found_msg, valid_keys):
    """크롤링 → 다운로드 → 크롭 → 디스크 저장. 성공 시 메타데이터 dict 반환."""
    data, error_msg = _fetch_menu_sync(target_year, target_week, not_found_msg)
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


def _load_meta_sync(key):
    """디스크에서 메타데이터 읽기 (없으면 None)"""
    path = meta_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


async def ensure_week_cache(target_year, target_week, not_found_msg):
    """
    해당 주차 캐시를 보장. (key, meta, error_msg) 반환.
    이미 캐시가 있으면 네트워크/CPU 작업 없이 즉시 반환.
    """
    key = make_key(target_year, target_week)
    now = datetime.datetime.now(KST)
    valid = _valid_keys(now)

    # RAM 메모에 있으면 바로 반환
    if key in _meta_memo:
        return key, _meta_memo[key], None

    async with _cache_lock:
        # 락 대기 중 다른 요청이 만들었을 수 있으니 재확인
        if key in _meta_memo:
            return key, _meta_memo[key], None

        meta = await asyncio.to_thread(_load_meta_sync, key)
        if meta is not None and meta.get("cache_ver") != CACHE_VERSION:
            meta = None  # 구버전 캐시 → 새 방식으로 재생성
        if meta is None:
            meta, error_msg = await asyncio.to_thread(
                _build_week_cache_sync, key, target_year, target_week, not_found_msg, valid
            )
            if error_msg:
                return key, None, error_msg

        # 지난 주차 메모 정리
        for old in [k for k in _meta_memo if k not in valid]:
            del _meta_memo[old]

        _meta_memo[key] = meta
        return key, meta, None


# --------------------------------------------------------
# 전체 식단표 전송 헬퍼 (/이번주, /다음주 공용)
# --------------------------------------------------------

async def send_full_sheet(interaction, key, meta, title):
    path = full_image_path(key, meta['ext'])
    if not os.path.exists(path):
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


@bot.tree.command(name="오점뭐", description="오늘 점심 메뉴를 보여줍니다. (평일 전용)")
async def today_lunch(interaction: discord.Interaction):
    now = datetime.datetime.now(KST)
    weekday = now.weekday()  # 0=월 ~ 6=일

    # 주말엔 오늘 점심이 없음
    if weekday >= 5:
        await interaction.response.send_message(
            "🛌 주말에는 구내식당이 쉬어요.\n다음 주 메뉴는 `/다음주`, 이번 주 메뉴는 `/이번주` 로 확인하세요!"
        )
        return

    await interaction.response.defer()

    # 이번 주 식단 = 저번 주 금요일에 올라온 게시물 (금요일에 조회해도 여전히 이번 주 것)
    target_year, target_week = this_week_target(now)
    key, meta, error_msg = await ensure_week_cache(
        target_year, target_week,
        f"오늘({now.strftime('%Y-%m-%d')})에 해당하는 식단표가 블로그에 없습니다."
    )

    if error_msg:
        await interaction.followup.send(f"⚠️ **{error_msg}**")
        return

    try:
        path = day_image_path(key, weekday)
        if not os.path.exists(path):
            await interaction.followup.send(
                f"❌ 이미지 크롭 실패\n\n직접 확인: {meta['post_url']}"
            )
            return

        today_name = WEEKDAY_NAMES[weekday]
        today_str = now.strftime('%Y-%m-%d')

        embed = discord.Embed(
            title=f"🍚 오늘의 점심 메뉴 ({today_name})",
            description=f"**{today_str}** 메가스터디 구내식당",
            color=0x2ecc71,
            url=meta['post_url']
        )

        filename = f"lunch_menu_{today_str}.png"
        image_file = discord.File(path, filename=filename)

        embed.set_image(url=f"attachment://{filename}")
        embed.add_field(
            name="📎 전체 메뉴 보기",
            value=f"[블로그에서 보기]({meta['post_url']})",
            inline=False
        )
        embed.set_footer(text=f"{meta['week_num']}주차 식단표 · 전체 메뉴는 /이번주")

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
    key, meta, error_msg = await ensure_week_cache(
        target_year, target_week,
        "이번 주 식단표를 블로그에서 찾을 수 없습니다."
    )

    if error_msg:
        await interaction.followup.send(f"⚠️ **{error_msg}**")
        return

    await send_full_sheet(interaction, key, meta, "📅 이번 주 전체 메뉴표")


@bot.tree.command(name="다음주", description="다음 주 전체 식단표를 보여줍니다. (매주 금요일 업로드 후 조회 가능)")
async def next_week(interaction: discord.Interaction):
    await interaction.response.defer()

    now = datetime.datetime.now(KST)
    target_year, target_week = next_week_target(now)
    key, meta, error_msg = await ensure_week_cache(
        target_year, target_week,
        "다음 주 식단표가 아직 올라오지 않았습니다.\n보통 **금요일 오전**에 블로그에 올라와요! 🕐"
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


@bot.tree.command(name="디버그", description="[서버 오너 전용] 이미지 URL과 월~금 크롭 결과를 모두 확인합니다.")
@app_commands.guild_only()
async def debug(interaction: discord.Interaction):

    # DM 에서는 guild 가 None 이라 오너 체크가 통과돼 버림 → guild_only + 이중 확인
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("⛔ 이 명령어는 서버 오너만 사용 가능.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    now = datetime.datetime.now(KST)
    target_year, target_week = this_week_target(now)
    key, meta, error_msg = await ensure_week_cache(
        target_year, target_week,
        "이번 주 식단표를 블로그에서 찾을 수 없습니다."
    )

    if error_msg:
        await interaction.followup.send(f"⚠️ {error_msg}", ephemeral=True)
        return

    debug_msg = f"""**디버그 정보** (이번 주: {key})
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


# ==========================================
# 실행
# ==========================================
if __name__ == "__main__":
    bot.run(load_token())
