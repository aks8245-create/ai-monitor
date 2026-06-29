# -*- coding: utf-8 -*-
"""
AI 노출 모니터링 — Streamlit 앱
================================
GPT(웹검색) + Gemini(grounding)에 질문을 던져, 설정한 병원이 어떻게
노출되는지 분석하고(노출 · 사이트/블로그/유튜브 인용 · 순위 · 함께 언급된
병원 · 발췌), 점유율/출처 요약과 함께 카드로 보여주고, HTML·CSV 리포트로
저장하며, 선택 시 구글시트에 기록해 월별 추이를 그린다.

배포(요약)
  1) GitHub repo에 streamlit_app.py, analysis.py, requirements.txt 업로드
  2) share.streamlit.io 에서 배포
  3) 앱 Settings → Secrets 에 입력:
        OPENAI_API_KEY = "sk-..."
        GEMINI_API_KEY = "..."
        APP_PASSWORD   = "비밀번호"
     (구글시트 추이 기능을 쓸 때만 추가)
        SHEET_ID = "구글시트 문서 ID"
        [gcp_service_account]   ← 서비스계정 JSON 내용
        ...

※ API/UI/구글시트 부분은 작성자 환경에서 실제 실행 검증하지 못했다.
  analysis.py 의 분석/집계 로직은 별도 테스트로 검증됨.
"""
import os
import re
import json
from datetime import datetime

import streamlit as st
import analysis as A

OPENAI_MODEL = "gpt-5.5"
GEMINI_MODEL = "gemini-3.5-flash"

PRESETS = {
    "어니스트여성의원": dict(
        name="어니스트여성의원",
        aliases="어니스트 여성의원, 어니스트클리닉, Honest Women's Clinic",
        site="honestclinic.com",
        blogs="honestclinic, honest0601",
        youtube="어니스트TV, Honest TV, honestclinic, 닥터 조혜진, 조혜진",
        competitors="르샘여성의원, 에이스여성의원, 로앤산부인과 여의도점, 여의도성모병원",
    ),
    "(빈 양식 — 새 병원)": dict(
        name="", aliases="", site="", blogs="", youtube="", competitors=""),
}

st.set_page_config(page_title="AI 노출 모니터링", page_icon="🔎", layout="centered")


def sec(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return default


# ── 비밀번호 잠금 ──────────────────────────────────────────
def check_password():
    pw = sec("APP_PASSWORD")
    if not pw:
        st.warning("APP_PASSWORD 미설정 — Secrets에 비밀번호를 추가하세요.")
        return True
    if st.session_state.get("auth"):
        return True
    with st.form("login"):
        x = st.text_input("비밀번호", type="password")
        ok = st.form_submit_button("입장")
    if ok:
        if x == pw:
            st.session_state["auth"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    return st.session_state.get("auth", False)


if not check_password():
    st.stop()


# ── 클라이언트 ─────────────────────────────────────────────
@st.cache_resource
def get_openai():
    try:
        from openai import OpenAI
        key = sec("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if key:
            return OpenAI(api_key=key)
    except Exception as e:
        st.warning(f"OpenAI 초기화 실패: {e}")
    return None


@st.cache_resource
def get_gemini():
    try:
        from google import genai
        key = sec("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
        if key:
            return genai.Client(api_key=key)
    except Exception as e:
        st.warning(f"Gemini 초기화 실패: {e}")
    return None


# ── 구글시트 (선택) ────────────────────────────────────────
SHEET_HEADER = ["날짜", "병원", "플랫폼", "분류", "쿼리", "노출", "사이트",
                "블로그", "유튜브", "유튜브우리", "순위", "총병원", "함께언급",
                "출처도메인", "실행시각"]


@st.cache_resource
def _open_sheet():
    info = sec("gcp_service_account")
    sid = sec("SHEET_ID")
    if not info or not sid:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        dict(info), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(sid)


def _ws_title(clinic):
    safe = re.sub(r'[:\\/?*\[\]]', '', clinic or '').strip()[:90]
    return f"data_{safe}" if safe else "data"


@st.cache_resource
def get_ws(clinic):
    """병원별 탭(워크시트)을 열거나 새로 만든다. 클라이언트별 데이터 분리."""
    try:
        sh = _open_sheet()
        if sh is None:
            return None
        import gspread
        title = _ws_title(clinic)
        try:
            ws = sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=1000, cols=20)
            ws.append_row(SHEET_HEADER)
        if not ws.get_all_values():
            ws.append_row(SHEET_HEADER)
        return ws
    except Exception as e:
        st.sidebar.warning(f"구글시트 연결 실패: {e}")
        return None


CONFIG_HEADER = ["병원", "aliases", "site", "blogs", "youtube", "competitors", "queries"]


@st.cache_resource
def get_config_ws():
    try:
        sh = _open_sheet()
        if sh is None:
            return None
        import gspread
        try:
            cws = sh.worksheet("config")
        except gspread.exceptions.WorksheetNotFound:
            cws = sh.add_worksheet(title="config", rows=200, cols=10)
            cws.append_row(CONFIG_HEADER)
        if not cws.get_all_values():
            cws.append_row(CONFIG_HEADER)
        return cws
    except Exception:
        return None


def save_config(cws, vals):
    # vals: [병원, aliases, site, blogs, youtube, competitors, queries] — 항상 추가(최신이 우선)
    cws.append_row(vals, value_input_option="USER_ENTERED")


def load_config(cws, name):
    found = None
    for i, r in enumerate(cws.get_all_values()):
        if i == 0 or not r or r[0] != name:
            continue
        rr = (r + [""] * 7)[:7]
        found = dict(aliases=rr[1], site=rr[2], blogs=rr[3],
                     youtube=rr[4], competitors=rr[5], queries=rr[6])
    return found   # 같은 병원이 여러 번 저장됐으면 마지막(최신)


def log_to_sheet(ws, entries, today, clinic, run_ts=""):
    rows = []
    for q, cat, prs in entries:
        for r in prs:
            rows.append([
                today, clinic, r.get("plat", ""), cat, q,
                "O" if r.get("exposed") else "X",
                "O" if r.get("site") else "X",
                "O" if r.get("blog") else "X",
                "O" if r.get("youtube") else "X",
                "O" if r.get("youtube_ours") else "X",
                r.get("rank") or "", r.get("total") or "",
                " | ".join(r.get("others") or []),
                " | ".join(sorted(set(r.get("domains") or []))),
                run_ts,
            ])
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


# ── 검색 호출 ──────────────────────────────────────────────
def q_openai(client, query):
    resp = client.responses.create(
        model=OPENAI_MODEL,
        tools=[{"type": "web_search", "user_location": {
            "type": "approximate", "country": "KR", "city": "Seoul",
            "region": "Seoul", "timezone": "Asia/Seoul"}}],
        include=["web_search_call.action.sources"],
        input=query,
    )
    text = getattr(resp, "output_text", "") or ""
    cits = []
    for item in getattr(resp, "output", []) or []:
        it = getattr(item, "type", None)
        if it == "message":
            for b in getattr(item, "content", []) or []:
                for ann in getattr(b, "annotations", []) or []:
                    if getattr(ann, "type", None) == "url_citation":
                        cits.append({"url": getattr(ann, "url", ""),
                                     "title": getattr(ann, "title", "")})
        if it == "web_search_call":
            act = getattr(item, "action", None)
            for s in (getattr(act, "sources", None) or []):
                url = getattr(s, "url", None) or (s.get("url") if isinstance(s, dict) else None)
                ttl = getattr(s, "title", None) or (s.get("title") if isinstance(s, dict) else "")
                if url:
                    cits.append({"url": url, "title": ttl or ""})
    return text, cits


def q_gemini(client, query):
    from google.genai import types
    cfg = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())])
    resp = client.models.generate_content(model=GEMINI_MODEL, contents=query, config=cfg)
    text = getattr(resp, "text", "") or ""
    cits = []
    for cand in (getattr(resp, "candidates", None) or []):
        gm = getattr(cand, "grounding_metadata", None)
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            u = getattr(web, "uri", None)
            t = getattr(web, "title", None)
            if u:
                cits.append({"url": u, "title": t or ""})
    return text, cits


def extract_order_llm(gem, answer_text):
    try:
        prompt = ("다음 답변에서 추천·언급된 병원 이름만 등장/추천 순서대로 "
                  "JSON 배열로 출력하세요. 병원이 없으면 []. 설명 없이 JSON만.\n\n답변:\n"
                  + answer_text)
        resp = gem.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw = (getattr(resp, "text", "") or "").strip().replace("```json", "").replace("```", "").strip()
        arr = json.loads(raw)
        return [str(x) for x in arr] if isinstance(arr, list) else None
    except Exception:
        return None


def build_row(plat, text, cits, cfg, accurate, gem):
    aliases = cfg["aliases"]
    cl = A.classify_citations(cits, cfg["site"], cfg["blogs"], cfg["youtube"], aliases)
    if accurate and gem:
        ordered = extract_order_llm(gem, text)
        if ordered is not None:
            rank, total, others = A.rank_from_names(ordered, aliases)
        else:
            rank, total, others = A.rank_free(text, aliases, cfg["competitors"])
    else:
        rank, total, others = A.rank_free(text, aliases, cfg["competitors"])
    return dict(plat=plat, exposed=A.name_mentioned(text, aliases),
                site=cl["site"], blog=cl["blog"], youtube=cl["youtube"],
                youtube_ours=cl["youtube_ours"], rank=rank, total=total,
                others=others, domains=cl["domains"],
                excerpt=A.extract_excerpt(text, aliases, 320), err="")


def run_query(query, cfg, accurate):
    oai, gem = get_openai(), get_gemini()
    rows = []
    for plat, fn, client in [("GPT", q_openai, oai), ("Gemini", q_gemini, gem)]:
        if not client:
            continue
        try:
            text, cits = fn(client, query)
            rows.append(build_row(plat, text, cits, cfg, accurate, gem))
        except Exception as e:
            rows.append(dict(plat=plat, exposed=False, site=False, blog=False,
                             youtube=False, youtube_ours=False, rank=None, total=0,
                             others=[], domains=[], excerpt="", err=f"ERROR: {e}"))
    return rows


# ── 사이드바: 병원 설정 ────────────────────────────────────
DEFAULT_QUERIES = "\n".join([
    "여의도 산부인과 추천", "영등포 산부인과 잘하는 곳", "여의도 여의사 산부인과",
    "비수술 요실금 치료 병원 서울", "반복되는 질염 잘 보는 병원",
])

st.sidebar.header("병원 설정")
preset = st.sidebar.selectbox("프리셋", list(PRESETS.keys()))
p = PRESETS[preset]

# 프리셋이 바뀌면 입력 필드를 그 프리셋 값으로 초기화
if st.session_state.get("_preset") != preset:
    st.session_state["_preset"] = preset
    st.session_state["f_name"] = p["name"]
    st.session_state["f_aliases"] = p["aliases"]
    st.session_state["f_site"] = p["site"]
    st.session_state["f_blogs"] = p["blogs"]
    st.session_state["f_youtube"] = p["youtube"]
    st.session_state["f_comps"] = p["competitors"]
st.session_state.setdefault("f_batch", DEFAULT_QUERIES)

cfg_ws = get_config_ws()


def _do_load():
    if cfg_ws is None:
        return
    loaded = load_config(cfg_ws, st.session_state.get("f_name", "").strip())
    if loaded:
        st.session_state["f_aliases"] = loaded["aliases"]
        st.session_state["f_site"] = loaded["site"]
        st.session_state["f_blogs"] = loaded["blogs"]
        st.session_state["f_youtube"] = loaded["youtube"]
        st.session_state["f_comps"] = loaded["competitors"]
        if loaded["queries"]:
            st.session_state["f_batch"] = loaded["queries"]
        st.session_state["_load_msg"] = "📂 저장된 설정을 불러왔어요."
    else:
        st.session_state["_load_msg"] = "저장된 설정이 없어요."


def _do_save():
    if cfg_ws is None:
        return
    nm = st.session_state.get("f_name", "").strip()
    if not nm:
        st.session_state["_load_msg"] = "병원 이름을 먼저 입력하세요."
        return
    save_config(cfg_ws, [nm, st.session_state.get("f_aliases", ""),
                         st.session_state.get("f_site", ""),
                         st.session_state.get("f_blogs", ""),
                         st.session_state.get("f_youtube", ""),
                         st.session_state.get("f_comps", ""),
                         st.session_state.get("f_batch", "")])
    st.session_state["_load_msg"] = "💾 설정을 저장했어요."


name = st.sidebar.text_input("병원 이름", key="f_name")
aliases_s = st.sidebar.text_input("이름 별칭 (쉼표)", key="f_aliases",
                                  help="같은 병원을 가리키는 다른 표기")
site = st.sidebar.text_input("사이트 도메인", key="f_site", help="예: honestclinic.com")
blogs_s = st.sidebar.text_input("네이버블로그 ID (쉼표)", key="f_blogs")
youtube = st.sidebar.text_input("유튜브 채널명/핸들", key="f_youtube")
comps_s = st.sidebar.text_area("경쟁 병원 (선택 · 쉼표/줄바꿈)", key="f_comps",
                               height=80,
                               help="비워둬도 됩니다. 비우면 답변에서 병원명을 자동으로 찾아냅니다.")

if cfg_ws is not None:
    b1, b2 = st.sidebar.columns(2)
    b1.button("💾 설정 저장", use_container_width=True, on_click=_do_save)
    b2.button("📂 불러오기", use_container_width=True, on_click=_do_load)
    if st.session_state.get("_load_msg"):
        st.sidebar.caption(st.session_state.pop("_load_msg"))

cfg = dict(
    name=name.strip(),
    aliases=A.split_list(name + "," + aliases_s),
    site=site.strip(),
    blogs=A.split_list(blogs_s),
    youtube=A.split_list(youtube),
    competitors=A.split_list(comps_s),
)

ws = get_ws(cfg["name"])
log_on = False
if ws is not None:
    log_on = st.sidebar.checkbox("구글시트에 기록", value=True)
else:
    st.sidebar.caption("구글시트 미연결 (추이 기능 끔)")


def maybe_log(entries, today, run_ts):
    if ws is not None and log_on and cfg["name"]:
        try:
            log_to_sheet(ws, entries, today, cfg["name"], run_ts)
        except Exception as e:
            st.warning(f"시트 기록 실패: {e}")


# ── 메인 ───────────────────────────────────────────────────
st.markdown(f"<style>{A.SUMMARY_CSS}{A.CARD_CSS}</style>", unsafe_allow_html=True)
st.title("🔎 AI 노출 모니터링")
st.caption(f"대상: {cfg['name'] or '(사이드바에서 병원을 설정하세요)'}")

c1, c2 = st.columns([4, 1])
query = c1.text_input("질문", placeholder="예: 여의도 산부인과 추천",
                      label_visibility="collapsed")
go = c2.button("검색", type="primary", use_container_width=True)
o1, o2 = st.columns(2)
is_control = o1.checkbox("병원명 직접 질의(대조군)")
accurate = o2.checkbox("정확 순위 모드 (AI 한 번 더 · 비용↑)")

if "results" not in st.session_state:
    st.session_state["results"] = []

now = datetime.now()
today = now.strftime("%Y-%m-%d")
run_ts = now.strftime("%Y-%m-%d %H:%M")   # 회차 구분자

if go and query.strip():
    if not cfg["name"]:
        st.error("먼저 사이드바에서 병원 이름을 설정하세요.")
    else:
        with st.spinner("검색 중… (몇 초~십몇 초)"):
            rows = run_query(query.strip(), cfg, accurate)
        entry = (query.strip(), "대조군" if is_control else "발견형", rows)
        st.session_state["results"].insert(0, entry)
        maybe_log([entry], today, run_ts)

results = st.session_state["results"]

# 요약 패널 (예쁜 화면)
if results:
    st.markdown(A.summary_html(results, cfg["aliases"]), unsafe_allow_html=True)
    st.divider()

# 개별 카드
for q, cat, rows in results:
    st.markdown(A.card_html(q, rows, cfg["aliases"]), unsafe_allow_html=True)

# 다운로드 / 비우기
if results:
    mode = "AI 순위(정확)" if accurate else "등장 순서(무료)"
    html = A.report_html(list(results), cfg["name"], cfg["aliases"], today, mode)
    csv = A.to_csv(list(results), today)
    d1, d2, d3 = st.columns(3)
    d1.download_button("리포트(HTML)", html,
                       file_name=f"ai_visibility_{cfg['name']}_{today}.html",
                       mime="text/html", use_container_width=True)
    d2.download_button("데이터(CSV)", csv,
                       file_name=f"ai_visibility_{cfg['name']}_{today}.csv",
                       mime="text/csv", use_container_width=True)
    if d3.button("결과 비우기", use_container_width=True):
        st.session_state["results"] = []
        st.rerun()

# 기본 세트 일괄 실행
with st.expander("기본 질문 세트 한 번에 돌리기 (선택)"):
    st.caption("한 줄에 질문 하나. 전부 '발견형'으로 검색됩니다. "
               "사이드바 [설정 저장]을 누르면 이 목록도 같이 저장돼요.")
    batch = st.text_area("질문 목록", key="f_batch", height=140)
    if st.button("세트 전체 실행"):
        if not cfg["name"]:
            st.error("병원 이름을 먼저 설정하세요.")
        else:
            qs = [x.strip() for x in batch.splitlines() if x.strip()]
            prog = st.progress(0.0)
            new_entries = []
            for i, qq in enumerate(qs):
                with st.spinner(f"({i+1}/{len(qs)}) {qq}"):
                    rows = run_query(qq, cfg, accurate)
                e = (qq, "발견형", rows)
                st.session_state["results"].insert(0, e)
                new_entries.append(e)
                prog.progress((i + 1) / len(qs))
            maybe_log(new_entries, today, run_ts)
            st.rerun()

# 월별 추이 (구글시트 연결 시)
if ws is not None:
    with st.expander("📈 월별 노출 추이 (구글시트 기록 기반)"):
        try:
            import pandas as pd
            vals = ws.get_all_values()
            if vals and vals[0][:2] == ["날짜", "병원"]:
                vals = vals[1:]
            cols = SHEET_HEADER
            data = [(row + [""] * len(cols))[:len(cols)] for row in vals]
            df = pd.DataFrame(data, columns=cols)
            if cfg["name"]:
                df = df[df["병원"] == cfg["name"]]
            df = df[df["분류"] == "발견형"].copy()

            # 시연용: 5월 테스트 데이터 한 줄 추가 버튼
            cset = st.columns([3, 1])
            cset[1].button(
                "5월 테스트 추가", use_container_width=True,
                help="시연용. 5월 데이터를 넣어 선이 이어지는 모습을 미리 봅니다. 시트에서 지울 수 있어요.",
                on_click=lambda: ws.append_rows([
                    ["2026-05-20", cfg["name"], "GPT", "발견형", "(테스트)", "O",
                     "X", "X", "X", "X", "", "", "", "", "2026-05-20 10:00 (테스트)"],
                    ["2026-05-20", cfg["name"], "Gemini", "발견형", "(테스트)", "O",
                     "X", "X", "X", "X", "", "", "", "", "2026-05-20 10:00 (테스트)"],
                ], value_input_option="USER_ENTERED") if cfg["name"] else None)

            if df.empty:
                st.info("아직 발견형 기록이 없습니다. 검색을 하면 누적됩니다.")
            else:
                # 회차 = 실행시각(없으면 날짜)
                df["회차"] = df["실행시각"].where(df["실행시각"].astype(str) != "", df["날짜"])
                runs = sorted(df["회차"].unique())
                chosen = cset[0].multiselect(
                    "표시할 회차 (체크 해제하면 그래프에서 숨김)", runs, default=runs)
                df = df[df["회차"].isin(chosen)]

                if df.empty:
                    st.info("표시할 회차를 1개 이상 선택하세요.")
                else:
                    df["ym"] = df["날짜"].astype(str).str[:7]
                    df["hit"] = (df["노출"] == "O").astype(int)
                    piv = (df.groupby(["ym", "플랫폼"])["hit"].mean()
                           .mul(100).round(0).unstack())
                    # X축을 "6월/7월"로 표시
                    piv.index = [f"{int(m[5:7])}월" if len(m) >= 7 else m for m in piv.index]
                    st.line_chart(piv)
                    st.caption("월별 발견형 노출률(%) · 선택한 회차 평균")
        except Exception as e:
            st.warning(f"추이 표시 실패: {e}")
