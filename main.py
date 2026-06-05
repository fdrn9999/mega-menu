# ==========================================
# 메가스터디 구내식당 메뉴 봇 (디스호스트 호스팅용)
# 사양: RAM 128MB / CPU 25% / 디스크 512MB 환경에 최적화
#
# 코랩 버전과의 차이점:
#  - nest_asyncio / Flask keep-alive 제거 (호스팅 환경에선 불필요)
#  - 토큰을 환경변수(DISCORD_TOKEN) 또는 token.txt 에서 읽음 (코드에 직접 X)
#  - 시간대를 Asia/Seoul 로 고정 (해외 서버에서도 한국 날짜/요일 기준으로 동작)
#  - 무거운 작업(크롤링/다운로드/크롭)은 주차당 1번만 실행하고 결과를 디스크에 캐시
#    → 명령어 호출 시에는 작은 PNG 파일만 읽어서 전송 (RAM/CPU 거의 안 씀)
#  - 이미지 후보 3개를 전부 받지 않고 Content-Length 로 크기만 확인 후 1개만 다운로드
#  - discord.py 메시지/멤버 캐시 비활성화로 상주 메모리 절약
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
from discord.ext import commands
from PIL import Image

# ==========================================
# [설정]
# ==========================================
BLOG_ID = 'megafs01'
CATEGORY_NO = '41'
TITLE_KEYWORD = '[메가스터디 구내식당]'
KST = ZoneInfo("Asia/Seoul")
REQUEST_TIMEOUT = 10          # 초
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024  # 이미지 다운로드 상한 12MB (메모리 보호)

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
# (message_content 같은 특권 인텐트는 개발자 포털 설정도 필요해서 오히려 실행이 막힐 수 있음)
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
# 캐시 파일 경로 헬퍼
# --------------------------------------------------------

def week_key(now: datetime.datetime) -> str:
    """예: 2026-W23"""
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


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
    저장 후 원본 비트맵은 즉시 해제 (128MB RAM 보호).
    """
    saved = 0
    img = Image.open(io.BytesIO(image_bytes))
    try:
        width, height = img.size

        # 크롭 좌표 기준 (사용자가 식단표 레이아웃에 맞춰 튜닝한 값 그대로 유지)
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
        return [
            base_url,
            f"{base_url}?type=w966",
            f"{base_url}?type=w2",
        ]

    return [base_url]


def _fetch_menu_sync():
    """이번 주차 식단표 게시물을 찾아 메타데이터 + 이미지 URL 목록 반환"""
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

        today = datetime.datetime.now(KST)
        current_iso_year, current_iso_week, _ = today.isocalendar()
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

            if post_iso_year == current_iso_year and post_iso_week == current_iso_week:
                target_post = post
                break

        if not target_post:
            return None, f"오늘({today.strftime('%Y-%m-%d')})에 해당하는 식단표가 블로그에 없습니다."

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
            "week_num": current_iso_week,
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
    (예전처럼 3개를 전부 받아 비교하지 않음 → 메모리/트래픽 절약)
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

    # 2) 가장 큰 것부터 시도
    for _, url in sorted(candidates, reverse=True):
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
#  - 새 주차 캐시를 만들 때 이전 주차 파일은 삭제 (디스크 512MB 보호)
# --------------------------------------------------------

_cache_lock = asyncio.Lock()
_meta_memo = {"key": None, "meta": None}  # 메타데이터(작은 dict)만 RAM에 유지


def _cleanup_old_cache(current_key):
    """현재 주차가 아닌 캐시 파일 전부 삭제"""
    if not os.path.isdir(CACHE_DIR):
        return
    for name in os.listdir(CACHE_DIR):
        if name.startswith("menu_") and f"menu_{current_key}_" not in name:
            try:
                os.remove(os.path.join(CACHE_DIR, name))
            except OSError:
                pass


def _build_week_cache_sync(key):
    """크롤링 → 다운로드 → 크롭 → 디스크 저장. 성공 시 메타데이터 dict 반환."""
    data, error_msg = _fetch_menu_sync()
    if error_msg:
        return None, error_msg

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
    _cleanup_old_cache(key)

    # 원본 저장 (주말용 전체 메뉴표)
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
    }

    with open(meta_path(key), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    # 원본 바이트 즉시 해제
    del image
    gc.collect()

    log.info("식단표 캐시 생성: %s (원본 %.1f KB, 크롭 %d개)", key, size / 1024, saved)
    return meta, None


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


async def ensure_week_cache():
    """
    이번 주차 캐시를 보장. (meta, error_msg) 반환.
    이미 캐시가 있으면 네트워크/CPU 작업 없이 즉시 반환.
    """
    key = week_key(datetime.datetime.now(KST))

    # RAM 메모(작은 dict)에 있으면 바로 반환
    if _meta_memo["key"] == key and _meta_memo["meta"]:
        return _meta_memo["meta"], None

    async with _cache_lock:
        # 락 대기 중 다른 요청이 만들었을 수 있으니 재확인
        if _meta_memo["key"] == key and _meta_memo["meta"]:
            return _meta_memo["meta"], None

        meta = await asyncio.to_thread(_load_meta_sync, key)
        if meta is None:
            meta, error_msg = await asyncio.to_thread(_build_week_cache_sync, key)
            if error_msg:
                return None, error_msg

        _meta_memo["key"] = key
        _meta_memo["meta"] = meta
        return meta, None


# --------------------------------------------------------
# 봇 이벤트 / 명령어
# --------------------------------------------------------

@bot.event
async def on_ready():
    log.info("✅ 로그인 성공: %s", bot.user)


@bot.tree.command(name="메가스터디", description="오늘 날짜에 해당하는 점심 메뉴를 보여줍니다.")
async def megastudy(interaction: discord.Interaction):
    await interaction.response.defer()

    meta, error_msg = await ensure_week_cache()

    if error_msg:
        await interaction.followup.send(f"⚠️ **{error_msg}**")
        return

    try:
        key = week_key(datetime.datetime.now(KST))
        today = datetime.datetime.now(KST)
        weekday = today.weekday()  # 0=월 ~ 6=일

        # 주말인 경우 (토, 일) — 전체 메뉴표 표시
        if weekday >= 5:
            path = full_image_path(key, meta['ext'])
            if not os.path.exists(path):
                await interaction.followup.send(
                    f"❌ 캐시된 이미지가 없습니다.\n\n직접 확인: {meta['post_url']}"
                )
                return

            embed = discord.Embed(
                title="📅 다음 주 전체 메뉴표",
                description=f"**{meta['week_num']}주차 식단표** (게시일: {meta['date']})\n주말이라 다음 주 전체 메뉴를 보여드립니다.",
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

            await interaction.followup.send(embed=embed, file=image_file)
            return

        # 평일인 경우 - 미리 크롭해둔 오늘 메뉴 파일 전송
        path = day_image_path(key, weekday)
        if not os.path.exists(path):
            await interaction.followup.send(
                f"❌ 이미지 크롭 실패\n\n직접 확인: {meta['post_url']}"
            )
            return

        today_name = WEEKDAY_NAMES[weekday]
        today_str = today.strftime('%Y-%m-%d')

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
        embed.set_footer(text=f"{meta['week_num']}주차 식단표")

        await interaction.followup.send(embed=embed, file=image_file)

    except Exception as e:
        log.exception("메뉴 처리 실패")
        await interaction.followup.send(
            f"❌ 이미지 처리 실패: {e}\n\n직접 확인: {meta['post_url']}"
        )


@bot.tree.command(name="디버그", description="[서버 오너 전용] 이미지 URL과 월~금 크롭 결과를 모두 확인합니다.")
async def debug(interaction: discord.Interaction):

    if interaction.guild and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("⛔ 이 명령어는 서버 오너만 사용 가능.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    meta, error_msg = await ensure_week_cache()

    if error_msg:
        await interaction.followup.send(f"⚠️ {error_msg}", ephemeral=True)
        return

    key = week_key(datetime.datetime.now(KST))

    debug_msg = f"""**디버그 정보**
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
