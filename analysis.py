# -*- coding: utf-8 -*-
"""
analysis.py — AI 노출 모니터링 분석 로직 (UI/API 비의존, 순수 함수)
streamlit_app.py 가 이 모듈을 import 해서 사용한다. 단독 테스트 가능.
"""
import re
import html as _html

# 병원명으로 보기 어려운 일반 접두어 (오탐 제거)
GENERIC_PREFIX = {
    "이", "그", "저", "우리", "동네", "근처", "주변", "가까운", "해당",
    "여러", "다른", "일부", "종합", "지역", "개인", "대학", "각", "좋은",
    "유명", "추천", "전문", "최고", "신규",
}
CLINIC_SUF = (r'(여성의원|여성병원|산부인과의원|산부인과|성형외과|피부과|'
              r'한의원|치과|안과|병원|의원|클리닉)')
_CLINIC_RE = re.compile(r'([가-힣A-Za-z][가-힣A-Za-z0-9]{0,10})' + CLINIC_SUF)


# 곡선 따옴표/백틱 → 곧은 따옴표 (GPT 답변의 ' 와 설정의 ' 차이 흡수)
_CANON = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'",
                        "\u201c": '"', "\u201d": '"'})


def _canon(s):
    return (s or "").translate(_CANON)


def norm(s):
    # 따옴표 통일 + 소문자 + 아포스트로피 제거 + 공백 제거
    return "".join(_canon(s).lower().replace("'", "").split())


def split_list(s):
    return [x.strip() for x in (s or "").replace("\n", ",").split(",") if x.strip()]


def domain_of(url):
    m = re.match(r'https?://([^/]+)', url or "")
    d = m.group(1) if m else (url or "")
    return d[4:] if d.startswith("www.") else d


def name_mentioned(text, aliases):
    n = norm(text)
    return any(norm(a) in n for a in aliases if a.strip())


def is_target(name, aliases):
    nn = norm(name)
    for a in aliases:
        na = norm(a)
        if na and (na in nn or nn in na):
            return True
    return False


def find_clinic_mentions(text):
    """병원명 패턴 (이름+접미어) 추출 → [(pos, 이름)]. 오탐 접두어 제거."""
    out = []
    for m in _CLINIC_RE.finditer(text or ""):
        pre, suf = m.group(1), m.group(2)
        if pre in GENERIC_PREFIX:
            continue
        out.append((m.start(), pre + suf))
    return out


def mentions_ordered(text, aliases, competitors):
    """대상 병원 첫 등장 위치 + 다른 병원들(등장순). returns (target_pos|None, [(name,pos)])."""
    text = text or ""
    ctext = _canon(text)  # 따옴표 통일 (길이 동일 → 위치 그대로 유효)
    target_pos = None
    for a in aliases:
        a = a.strip()
        if not a:
            continue
        i = ctext.find(_canon(a))
        if i >= 0:
            target_pos = i if target_pos is None else min(target_pos, i)

    others = {}
    for c in competitors:
        c = c.strip()
        if not c:
            continue
        i = ctext.find(_canon(c))
        if i >= 0:
            others[c] = min(others.get(c, i), i)

    for pos, name in find_clinic_mentions(text):
        if is_target(name, aliases):
            continue
        merged = False
        for k in list(others):
            if norm(k) == norm(name):
                others[k] = min(others[k], pos)
                merged = True
                break
        if not merged:
            others[name] = min(others.get(name, pos), pos)

    others_sorted = sorted(others.items(), key=lambda kv: kv[1])
    return target_pos, others_sorted


def rank_free(text, aliases, competitors):
    """등장 순서 기반 순위. returns (rank|None, total, [others])."""
    target_pos, others = mentions_ordered(text, aliases, competitors)
    others_names = [n for n, _ in others]
    if target_pos is None:
        return None, len(others), others_names
    before = sum(1 for _, p in others if p < target_pos)
    return before + 1, len(others) + 1, others_names


def rank_from_names(ordered_names, aliases):
    """LLM이 뽑은 순서 리스트 기반 순위."""
    others = [x for x in ordered_names if not is_target(x, aliases)]
    for i, n in enumerate(ordered_names):
        if is_target(n, aliases):
            return i + 1, len(ordered_names), others
    return None, len(ordered_names), others


def extract_excerpt(text, aliases, maxlen=300):
    """대상 병원이 언급된 문장 발췌. 없으면 도입부."""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    ctext = _canon(text)
    pos = -1
    for a in aliases:
        a = a.strip()
        if a:
            j = ctext.find(_canon(a))
            if j >= 0:
                pos = j
                break
    if pos < 0:
        return text[:maxlen]
    start = max(text.rfind(". ", 0, pos), text.rfind("다. ", 0, pos))
    start = 0 if start < 0 else start + 1
    end = text.find(". ", pos)
    end = len(text) if end < 0 else end + 1
    return text[start:end].strip()[:maxlen]


def classify_citations(citations, site, blogs, youtube, aliases):
    """citations: [{url,title}]. 사이트/블로그/유튜브 인용 분류."""
    res = dict(site=False, blog=False, youtube=False, youtube_ours=False, domains=[])
    for c in citations or []:
        u = (c.get("url") or "").lower()
        t = c.get("title") or ""
        if not u:
            continue
        res["domains"].append(domain_of(u))
        if site and site.lower() in u:
            res["site"] = True
        if "naver.com" in u and any(b.lower() in u for b in blogs if b.strip()):
            res["blog"] = True
        if "youtube.com" in u or "youtu.be" in u:
            res["youtube"] = True
            tn = norm(t)
            if (youtube and norm(youtube) in tn) or any(norm(a) in tn for a in aliases if a.strip()):
                res["youtube_ours"] = True
    return res


# ── HTML 렌더 (인라인 스타일 — Streamlit에서도 색 적용됨) ──────
_S_HL = ('background:#e1f5ee;color:#0f6e56;padding:0 3px;border-radius:3px;font-weight:600')
_S_ON = ('display:inline-block;font-size:12px;font-weight:500;padding:3px 10px;'
         'border-radius:8px;background:#e1f5ee;color:#0f6e56')
_S_OFF = ('display:inline-block;font-size:12px;font-weight:500;padding:3px 10px;'
          'border-radius:8px;background:#f1efe8;color:#888780')
_S_BDB = ('display:inline-block;font-size:11px;padding:2px 7px;margin-left:5px;'
          'border-radius:6px;background:#e6f1fb;color:#185fa5')
_S_BDR = ('display:inline-block;font-size:11px;padding:2px 7px;margin-left:5px;'
          'border-radius:6px;background:#fbe9e6;color:#a53d18')
_S_RK = 'display:inline-block;font-size:12px;margin-left:8px;color:#5b5a55;font-weight:500'
_S_ANS = 'font-size:13px;color:#55544f;line-height:1.65;margin-top:6px'
_S_OTH = 'font-size:12px;color:#8a8980;margin-top:5px'
_S_PL = 'font-size:11px;color:#6b6a66;font-weight:600;width:54px;flex-shrink:0'


def _hl(text, aliases):
    safe = _html.escape(text or "")
    for a in aliases:
        a = a.strip()
        if not a:
            continue
        safe = re.sub("(" + re.escape(_html.escape(a)) + ")",
                      rf'<span style="{_S_HL}">\1</span>', safe)
    return safe


def _badges(r):
    out = []
    if r.get("site"):
        out.append(f'<span style="{_S_BDB}">사이트</span>')
    if r.get("blog"):
        out.append(f'<span style="{_S_BDB}">네이버블로그</span>')
    if r.get("youtube"):
        label = "유튜브(우리채널)" if r.get("youtube_ours") else "유튜브"
        out.append(f'<span style="{_S_BDR}">{label}</span>')
    return "".join(out)


def _rank_txt(r):
    if r.get("err"):
        return ""
    if r.get("rank"):
        return f'<span style="{_S_RK}">우리 {r["rank"]}번째 · 총 {r["total"]}곳</span>'
    if r.get("exposed"):
        return f'<span style="{_S_RK}">순위 산정 불가</span>'
    return ""


def _platform_block(r, aliases):
    exposed = r.get("exposed")
    pill = (f'<span style="{_S_ON}">노출</span>' if exposed
            else f'<span style="{_S_OFF}">미노출</span>')
    badges = _badges(r)
    rank = _rank_txt(r)
    others = r.get("others") or []
    others_html = (f'<div style="{_S_OTH}">함께 언급: {_html.escape(", ".join(others))}</div>'
                   if others else "")
    if r.get("err"):
        ans = f'<div style="{_S_ANS};color:#a32d2d">{_html.escape(r["err"])}</div>'
    else:
        ans = f'<div style="{_S_ANS}">{_hl(r.get("excerpt", ""), aliases)}</div>'
    return (f'<div style="display:flex;gap:10px;margin-top:12px">'
            f'<span style="{_S_PL}">{r["plat"]}</span>'
            f'<div style="flex:1;min-width:0">{pill}{badges}{rank}{ans}{others_html}</div></div>')


def card_html(query, rows, aliases):
    inner = "".join(_platform_block(r, aliases) for r in rows)
    return (f'<div style="border:1px solid #e6e4dd;border-radius:12px;padding:14px 18px;'
            f'margin:10px 0;max-width:840px;font-family:Pretendard,-apple-system,'
            f'\'Malgun Gothic\',sans-serif">'
            f'<div style="font-size:15px;font-weight:600;color:#2c2c2a">'
            f'{_html.escape(query)}</div>{inner}</div>')


CARD_CSS = """
.qcard{border:1px solid #e6e4dd;border-radius:12px;padding:14px 18px;margin:10px 0;
  max-width:780px;font-family:Pretendard,-apple-system,'Malgun Gothic',sans-serif}
.qt{font-size:15px;font-weight:600;color:#2c2c2a}
.prow{display:flex;gap:10px;margin-top:12px}
.pl{font-size:11px;color:#6b6a66;font-weight:600;width:54px;flex-shrink:0}
.pbody{flex:1;min-width:0}
.on{display:inline-block;font-size:12px;font-weight:500;padding:3px 10px;border-radius:8px;
  background:#e1f5ee;color:#0f6e56}
.off{display:inline-block;font-size:12px;font-weight:500;padding:3px 10px;border-radius:8px;
  background:#f1efe8;color:#888780}
.bd{display:inline-block;font-size:11px;padding:2px 7px;margin-left:5px;border-radius:6px}
.bd-b{background:#e6f1fb;color:#185fa5}
.bd-r{background:#fbe9e6;color:#a53d18}
.rk{display:inline-block;font-size:12px;margin-left:8px;color:#5b5a55;font-weight:500}
.ans{font-size:13px;color:#55544f;line-height:1.65;margin-top:6px}
.ans.err{color:#a32d2d}
.oth{font-size:12px;color:#8a8980;margin-top:5px}
.hl{background:#e1f5ee;color:#0f6e56;padding:0 3px;border-radius:3px;font-weight:600}
"""


def report_html(rows_by_query, clinic_name, aliases, today, rank_mode_label):
    """rows_by_query: list of (query, category, [platform_row,...])."""
    disc = [t for t in rows_by_query if t[1] == "발견형"]

    def rate(plat):
        rs = [pr for _, _, prs in disc for pr in prs if pr["plat"] == plat]
        hit = sum(1 for pr in rs if pr.get("exposed"))
        n = len(rs)
        return hit, n, (round(hit / n * 100) if n else 0)

    g = rate("GPT")
    m = rate("Gemini")

    def section(cat):
        out = []
        for q, c, prs in rows_by_query:
            if c != cat:
                continue
            out.append(card_html(q, prs, aliases))
        return "\n".join(out) or '<div style="color:#999;font-size:13px">(없음)</div>'

    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>AI 노출 모니터링 {today}</title><style>
body{{font-family:Pretendard,-apple-system,'Malgun Gothic',sans-serif;background:#f5f5f3;
  color:#2c2c2a;margin:0;padding:32px}}
.wrap{{max-width:880px;margin:0 auto;background:#fff;border:1px solid #e6e4dd;
  border-radius:16px;padding:28px 32px}}
h1{{font-size:20px;font-weight:600;margin:0 0 2px}}
.date{{font-size:13px;color:#6b6a66;margin-bottom:6px}}
.mode{{font-size:12px;color:#9b9a94;margin-bottom:20px}}
h2{{font-size:15px;font-weight:600;margin:24px 0 8px}}
{SUMMARY_CSS}
{CARD_CSS}
</style></head><body><div class="wrap">
<h1>AI 노출 모니터링</h1>
<div class="date">{_html.escape(str(clinic_name))} · 검색일 {today}</div>
<div class="mode">순위 방식: {rank_mode_label}</div>
{summary_html(rows_by_query, aliases)}
<h2>발견형 쿼리</h2>
{section("발견형")}
<h2>대조군 쿼리</h2>
{section("대조군")}
</div></body></html>"""


# ── 집계 (점유율 / 인용 도메인) ─────────────────────────────
def share_of_voice(rows_by_query, aliases):
    """전체 결과에서 병원별 등장 횟수. returns ([(label,count)], total_slots)."""
    counts = {}
    slots = 0
    for _q, _cat, prs in rows_by_query:
        for r in prs:
            slots += 1
            if r.get("exposed"):
                counts["우리 병원"] = counts.get("우리 병원", 0) + 1
            seen = set()
            for o in (r.get("others") or []):
                key = o.strip()
                if not key or norm(key) in seen:
                    continue
                seen.add(norm(key))
                counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return items, slots


def domain_counts(rows_by_query, top=12):
    """인용 출처 도메인별 (결과 수 기준) 집계. 중계/노이즈 도메인 제외."""
    SKIP = {"vertexaisearch.cloud.google.com", "googleusercontent.com",
            "google.com", "bing.com"}
    counts = {}
    for _q, _cat, prs in rows_by_query:
        for r in prs:
            for d in set(r.get("domains") or []):
                if d and d not in SKIP and "vertexaisearch" not in d:
                    counts[d] = counts.get(d, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def to_csv(rows_by_query, today):
    import csv
    import io
    buf = io.StringIO()
    buf.write("\ufeff")  # 엑셀 한글 BOM
    w = csv.writer(buf)
    w.writerow(["검색일자", "플랫폼", "분류", "쿼리", "노출", "사이트인용",
                "블로그인용", "유튜브인용", "유튜브우리채널", "순위", "총병원수",
                "함께언급", "발췌", "비고"])
    for q, cat, prs in rows_by_query:
        for r in prs:
            w.writerow([
                today, r.get("plat", ""), cat, q,
                "O" if r.get("exposed") else "X",
                "O" if r.get("site") else "X",
                "O" if r.get("blog") else "X",
                "O" if r.get("youtube") else "X",
                "O" if r.get("youtube_ours") else "X",
                r.get("rank") or "", r.get("total") or "",
                " | ".join(r.get("others") or []),
                r.get("excerpt", ""), r.get("err", ""),
            ])
    return buf.getvalue()


# ── 요약 패널 (예쁜 화면) ───────────────────────────────────
SUMMARY_CSS = """
.sum{max-width:840px;font-family:Pretendard,-apple-system,'Malgun Gothic',sans-serif}
.sgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px}
.scard{background:linear-gradient(180deg,#fafaf8,#f3f3f0);border:1px solid #ecec== e6;
  border-radius:12px;padding:16px 18px}
.scard .lb{font-size:12px;color:#6b6a66;margin-bottom:6px}
.scard .nm{font-size:26px;font-weight:700;color:#2c2c2a}
.scard .nm small{font-size:14px;color:#9b9a94;font-weight:400}
.sec{font-size:13px;font-weight:700;color:#2c2c2a;margin:16px 0 8px}
.bar{display:flex;align-items:center;gap:10px;margin:5px 0;font-size:13px}
.bar .bn{width:140px;flex-shrink:0;color:#2c2c2a;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.bar .tr{flex:1;background:#f1efe8;border-radius:6px;height:16px;overflow:hidden}
.bar .fl{height:100%;border-radius:6px;background:#c2cbc6}
.bar.me .bn{font-weight:700;color:#0f6e56}
.bar.me .fl{background:#0f6e56}
.bar .ct{width:28px;text-align:right;color:#6b6a66}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:2px}
.chip{font-size:12px;background:#eef2f7;color:#3a4a5a;padding:4px 10px;border-radius:999px}
.chip b{color:#185fa5;margin-left:4px}
"""
SUMMARY_CSS = SUMMARY_CSS.replace("#ecec== e6", "#ececec")


def summary_html(rows_by_query, aliases):
    disc = [t for t in rows_by_query if t[1] == "발견형"]

    def rate(plat):
        rs = [pr for _, _, prs in disc for pr in prs if pr["plat"] == plat]
        hit = sum(1 for pr in rs if pr.get("exposed"))
        n = len(rs)
        return hit, n, (round(hit / n * 100) if n else 0)

    g, m = rate("GPT"), rate("Gemini")
    sov, _slots = share_of_voice(rows_by_query, aliases)
    doms = domain_counts(rows_by_query)

    maxc = max([c for _, c in sov], default=1) or 1
    bars = ""
    for label, c in sov[:8]:
        is_me = (label == "우리 병원")
        w = int(c / maxc * 100)
        fill = "#0f6e56" if is_me else "#c2cbc6"
        nm_style = ("width:140px;flex-shrink:0;white-space:nowrap;overflow:hidden;"
                    "text-overflow:ellipsis;"
                    + ("font-weight:700;color:#0f6e56" if is_me else "color:#2c2c2a"))
        bars += (
            f'<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:13px">'
            f'<span style="{nm_style}">{_html.escape(label)}</span>'
            f'<span style="flex:1;background:#f1efe8;border-radius:6px;height:16px;overflow:hidden">'
            f'<span style="display:block;height:100%;border-radius:6px;background:{fill};width:{w}%"></span></span>'
            f'<span style="width:28px;text-align:right;color:#6b6a66">{c}</span></div>')
    if not bars:
        bars = '<div style="color:#999;font-size:13px">데이터 없음</div>'

    chip_s = ('display:inline-block;font-size:12px;background:#eef2f7;color:#3a4a5a;'
              'padding:4px 10px;border-radius:999px;margin:0 7px 7px 0')
    chips = "".join(
        f'<span style="{chip_s}">{_html.escape(d)} <b style="color:#185fa5">{c}</b></span>'
        for d, c in doms)
    if not chips:
        chips = '<div style="color:#999;font-size:13px">데이터 없음</div>'

    scard = ('background:linear-gradient(180deg,#fafaf8,#f3f3f0);border:1px solid #ececec;'
             'border-radius:12px;padding:16px 18px')
    sec = 'font-size:13px;font-weight:700;color:#2c2c2a;margin:16px 0 8px'
    return (
        f'<div style="max-width:840px;font-family:Pretendard,-apple-system,\'Malgun Gothic\',sans-serif">'
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px">'
        f'<div style="{scard}"><div style="font-size:12px;color:#6b6a66;margin-bottom:6px">GPT 발견형 노출</div>'
        f'<div style="font-size:26px;font-weight:700;color:#2c2c2a">{g[0]}'
        f'<small style="font-size:14px;color:#9b9a94;font-weight:400"> / {g[1]} · {g[2]}%</small></div></div>'
        f'<div style="{scard}"><div style="font-size:12px;color:#6b6a66;margin-bottom:6px">Gemini 발견형 노출</div>'
        f'<div style="font-size:26px;font-weight:700;color:#2c2c2a">{m[0]}'
        f'<small style="font-size:14px;color:#9b9a94;font-weight:400"> / {m[1]} · {m[2]}%</small></div></div></div>'
        f'<div style="{sec}">노출 점유율 (등장 횟수)</div>{bars}'
        f'<div style="{sec}">AI가 자주 인용한 출처</div><div>{chips}</div></div>')
