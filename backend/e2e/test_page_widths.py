"""通しのテスト: 390px で、どの画面も右にはみ出さない（修正 1 回目 B15）。

seed_dummy のデータで管理者がログインし、ダッシュボードから画面の中のリンクをたどる
（URL の形ごとに 1 回。数字は同じ形とみなす）。はみ出した画面と、はみ出した要素を一覧にして
落とす。表は表の中で横に送る。
"""

import re
from collections import deque
from urllib.parse import urlparse

from playwright.sync_api import Page

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from e2e.test_phase2_gate import base_date, seeded  # noqa: F401  （fixture）

WIDTH = 390
LIMIT = 220  # たどる画面の数の上限
SKIP = re.compile(
    r"^/(admin|static|media)/|/logout/|/download|/pdf/|/file/|/attachments/\d+/|\.(pdf|xlsx|docx|zip)$"
)
OVERFLOW = """() => {
  const width = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth <= width) return [];
  return Array.from(document.querySelectorAll("body *"))
    .filter((el) => {
      const box = el.getBoundingClientRect();
      if (box.width === 0 || box.right <= width + 1) return false;
      for (let p = el.parentElement; p; p = p.parentElement) {
        const style = getComputedStyle(p);
        if (["auto", "scroll", "hidden"].includes(style.overflowX)) return false;
      }
      return true;
    })
    .slice(0, 3)
    .map((el) => `${el.tagName.toLowerCase()}.${el.className}#${el.id}`);
}"""


def shape(path: str, query: str) -> str:
    keys = sorted(part.split("=")[0] for part in query.split("&") if part)
    return re.sub(r"\d+", "N", path) + ("?" + "&".join(keys) if keys else "")


def test_no_page_overflows_at_390(
    page: Page,
    sign_in: SignIn,
    administrator: User,
    seeded: list[ForeignWorker],  # noqa: F811
) -> None:
    page.set_viewport_size({"width": WIDTH, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    origin = urlparse(base)
    queue = deque(["/", f"/workers/{seeded[1].pk}/", f"/workers/{seeded[8].pk}/", "/hosts/"])
    seen: set[str] = set()
    problems: list[str] = []
    visited = 0
    while queue and visited < LIMIT:
        path = queue.popleft()
        parsed = urlparse(path)
        key = shape(parsed.path, parsed.query)
        if key in seen or SKIP.search(parsed.path):
            continue
        seen.add(key)
        response = page.goto(base + path)
        visited += 1
        if (
            response is None
            or response.status != 200
            or "text/html" not in (response.headers.get("content-type", ""))
        ):
            continue
        offenders = page.evaluate(OVERFLOW)
        if offenders:
            problems.append(f"{path}: {offenders}")
        for href in page.eval_on_selector_all("a[href]", "links => links.map((a) => a.href)"):
            link = urlparse(href)
            if link.netloc != origin.netloc or not link.path:
                continue
            queue.append(link.path + (f"?{link.query}" if link.query else ""))
    print(f"visited {visited} pages")
    assert visited > 40, visited
    assert problems == [], "\n".join(problems)
