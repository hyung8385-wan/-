"""
정책단가 조회 프로그램 (Streamlit)
------------------------------------
- 직원 화면: 모델 / 가입유형 / 요금제 선택 → 최종 정책단가 자동 계산
- 관리자 화면: 정책 데이터(정책지 A/B/C ...) 추가/수정/삭제
- 데이터는 policy.db (SQLite) 파일에 저장됨. 프로그램을 껐다 켜도 데이터 유지됨.

실행 방법 (cmd 또는 PowerShell에서):
    pip install -r requirements.txt
    streamlit run app.py
"""

import base64
import datetime
import json
import os
import re
import sqlite3

import pandas as pd
import requests
import streamlit as st

DB_PATH = "policy.db"
API_KEY_PATH = "api_key.txt"  # 구글 Gemini API 키를 저장해두는 파일 (같은 폴더에 생성됨)
GEMINI_MODEL = "gemini-3.6-flash"  # 정책지 이미지 분석용 최신 Flash 모델

# ----------------------------------------------------------------------
# 1. DB 관련 함수
# ----------------------------------------------------------------------

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    """policies 테이블이 없으면 만든다. 예전 버전 DB라면 정책 기간(start_date/end_date) 컬럼을
    안전하게 추가한다 (기존에 저장된 데이터는 그대로 유지되고, 새 컬럼만 빈 값으로 추가됨)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,   -- 정책지 이름 (예: MWA-01 (도매 기본 정책))
            model TEXT NOT NULL,        -- 모델명
            join_type TEXT NOT NULL,    -- MNP / 기변 / 신규 등
            threshold INTEGER NOT NULL, -- 이 금액 "이상"부터 적용되는 요금제 기준(원)
            amount INTEGER NOT NULL,    -- 지급되는 정책단가(원)
            start_date TEXT,            -- 정책 적용 시작일 (YYYY-MM-DD), 없으면 NULL(제한없음)
            end_date TEXT               -- 정책 적용 종료일 (YYYY-MM-DD), 없으면 NULL(종료일 미정=계속 유효)
        )
        """
    )
    conn.commit()

    cur.execute("PRAGMA table_info(policies)")
    existing_cols = [row[1] for row in cur.fetchall()]
    if "start_date" not in existing_cols:
        cur.execute("ALTER TABLE policies ADD COLUMN start_date TEXT")
    if "end_date" not in existing_cols:
        cur.execute("ALTER TABLE policies ADD COLUMN end_date TEXT")
    conn.commit()
    conn.close()


def load_all() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM policies ORDER BY group_name, threshold", conn)
    conn.close()
    return df


def _clean_date_value(v) -> str | None:
    """표에서 편집된 날짜 값을 DB에 넣기 좋은 형태(문자열 또는 None)로 정리한다."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    v = str(v).strip()
    return v if v else None


def save_all(df: pd.DataFrame):
    """관리자 화면에서 편집한 표 전체를 DB에 덮어쓴다.
    모델명에 괄호가 있으면(예: SM-S942(7,8)NK) 저장 시 자동으로 여러 모델로 풀어서 저장한다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM policies")
    for _, row in df.iterrows():
        # 빈 행(신규 추가하다가 값을 안 채운 행)은 건너뜀
        if pd.isna(row.get("group_name")) or pd.isna(row.get("model")):
            continue
        start_date = _clean_date_value(row.get("start_date"))
        end_date = _clean_date_value(row.get("end_date"))
        for model_variant in expand_model_variants(str(row["model"])):
            cur.execute(
                "INSERT INTO policies (group_name, model, join_type, threshold, amount, start_date, end_date) VALUES (?,?,?,?,?,?,?)",
                (
                    str(row["group_name"]),
                    model_variant,
                    str(row["join_type"]),
                    int(row["threshold"]),
                    int(row["amount"]),
                    start_date,
                    end_date,
                ),
            )
    conn.commit()
    conn.close()


def insert_rows(rows: list[dict]):
    """사진에서 추출한 정책 행들을 기존 데이터에 추가(append)한다. 덮어쓰지 않음.
    모델명에 괄호가 남아있는 경우를 대비해 여기서도 한 번 더 자동으로 풀어서 저장한다."""
    conn = get_conn()
    cur = conn.cursor()
    for row in rows:
        start_date = _clean_date_value(row.get("start_date"))
        end_date = _clean_date_value(row.get("end_date"))
        for model_variant in expand_model_variants(str(row.get("model", ""))):
            cur.execute(
                "INSERT INTO policies (group_name, model, join_type, threshold, amount, start_date, end_date) VALUES (?,?,?,?,?,?,?)",
                (
                    str(row["group_name"]),
                    model_variant,
                    str(row["join_type"]),
                    int(row["threshold"]),
                    int(row["amount"]),
                    start_date,
                    end_date,
                ),
            )
    conn.commit()
    conn.close()


def calc_price(model: str, join_type: str, plan_fee: int):
    """선택한 조건에 맞는 정책지별 최고 구간 금액을 합산한다.
    오늘 날짜 기준으로 정책 적용기간(start_date~end_date)이 지났거나 아직 시작 전인 정책지는 제외한다."""
    df = load_all()
    df = df[(df["model"] == model) & (df["join_type"] == join_type)]

    today = datetime.date.today().isoformat()
    if "start_date" in df.columns:
        df = df[df["start_date"].isna() | (df["start_date"] <= today)]
    if "end_date" in df.columns:
        df = df[df["end_date"].isna() | (df["end_date"] >= today)]

    total = 0
    detail = []
    for group in sorted(df["group_name"].unique()):
        gdf = df[df["group_name"] == group]
        gdf = gdf[gdf["threshold"] <= plan_fee]
        if not gdf.empty:
            best = gdf.sort_values("threshold", ascending=False).iloc[0]
            total += int(best["amount"])
            detail.append(
                {
                    "정책지": group,
                    "적용 구간(이상)": f"{int(best['threshold']):,}원",
                    "금액": f"{int(best['amount']):,}원",
                }
            )
    return total, detail


def expand_model_variants(model: str) -> list[str]:
    """정책지 표의 모델명에 괄호가 있으면 여러 모델을 한 줄로 묶어서 표기한 것이므로,
    실제 모델명 여러 개로 풀어서 리스트로 돌려준다.

    예:
    - "SM-F971(6)NK"   -> ["SM-F971NK", "SM-F976NK"]
      (괄호 안이 숫자면, 바로 앞 숫자 한 자리를 괄호 안 숫자로 교체한 모델을 추가)
    - "SM-S942(7,8)NK" -> ["SM-S942NK", "SM-S947NK", "SM-S948NK"]
    - "AIP17(P,PM)"    -> ["AIP17", "AIP17P", "AIP17PM"]
      (괄호 안이 문자면, 원래 모델명 뒤에 이어붙인 모델을 추가)
    - 괄호가 없으면 원래 모델명 하나만 그대로 돌려준다.
    """
    model = (model or "").strip()
    if not model:
        return [model]

    match = re.match(r"^(.*)\(([^)]+)\)(.*)$", model)
    if not match:
        return [model]

    prefix, paren_content, suffix = match.groups()
    values = [v.strip() for v in paren_content.split(",") if v.strip()]
    if not values:
        return [model]

    if all(v.isdigit() for v in values) and prefix and prefix[-1].isdigit():
        # 예: SM-F971(6)NK -> 마지막 숫자 '1'을 '6'으로 교체 -> SM-F976NK
        prefix_wo_digit = prefix[:-1]
        base = prefix + suffix
        variants = [base] + [prefix_wo_digit + v + suffix for v in values]
    else:
        # 예: AIP17(P,PM) -> 뒤에 이어붙임 -> AIP17P, AIP17PM
        base = prefix + suffix
        variants = [base] + [prefix + v + suffix for v in values]

    # 중복 제거 (순서 유지)
    seen = set()
    result = []
    for m in variants:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def parse_korean_date(text: str) -> str | None:
    """'9월 1일', '2026-09-01', '2026.9.1' 같은 표기를 'YYYY-MM-DD' 형식으로 바꾼다.
    연도가 적혀있지 않으면 오늘 날짜 기준 연도를 사용한다.
    '개통'처럼 날짜가 아닌 문구뿐이면 None을 돌려준다 (= 정해진 날짜 없음)."""
    if not text:
        return None
    text = str(text).strip()
    if not text:
        return None

    today_year = datetime.date.today().year

    m = re.search(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
        if not m:
            return None
        y = today_year
        mo, d = int(m.group(1)), int(m.group(2))

    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def format_period(start_date, end_date) -> str:
    """정책 유효기간을 사람이 보기 좋은 문자열로 바꾼다."""
    s = _clean_date_value(start_date)
    e = _clean_date_value(end_date)
    if not s and not e:
        return "기간 제한 없음"
    return f"{s or '제한없음'} ~ {e or '진행중(종료일 미정)'}"


def extract_policy_code(title: str) -> str:
    """정책지 제목에서 앞부분의 '영어-숫자' 코드(예: MWA-01)만 뽑아낸다.
    이 코드를 기준으로 여러 사진에서 읽은 결과를 같은 표로 묶는다.
    코드 형식이 없으면 제목 전체를 그대로 코드로 쓴다."""
    title = (title or "").strip()
    m = re.match(r"^([A-Za-z]{1,10}-\d{1,10})", title)
    return m.group(1).upper() if m else title


# ----------------------------------------------------------------------
# 2. 사진(OCR) 관련 함수 — 구글 Gemini API(무료 등급)로 이미지 속 표를 읽어온다
# ----------------------------------------------------------------------

def get_saved_api_key() -> str:
    """API 키를 가져온다. 인터넷에 배포한 경우(Streamlit Cloud)는 'Secrets'에 저장된 값을,
    내 PC에서 직접 실행하는 경우는 같은 폴더의 api_key.txt를 사용한다."""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    if os.path.exists(API_KEY_PATH):
        with open(API_KEY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_api_key(key: str):
    try:
        with open(API_KEY_PATH, "w", encoding="utf-8") as f:
            f.write(key.strip())
    except Exception:
        pass  # 배포 환경 등 파일 저장이 불가능한 경우 조용히 넘어감 (Secrets 사용 권장)


def extract_policy_rows_from_image(image_bytes: bytes, media_type: str, api_key: str) -> list[dict]:
    """정책지 이미지에서 제목/모델/가입유형/요금제 기준/정책단가를 추출한다."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """
이 이미지는 휴대폰 판매 정책지(정책단가표)이다.
이미지 전체를 꼼꼼하게 읽고, 표에 있는 모든 모델/가입유형/요금제 기준/정책단가를 추출해줘.

반드시 JSON 배열만 출력하고 설명은 출력하지 마.

각 행은 아래 8개 필드를 가져야 한다.
- policy_title: 이미지 가장 상단의 제목 영역. 보통 좌측에 붉은색 코드(예: MWA-01)와 우측에 파란색 정책 설명(예: 도매 기본 정책)이 함께 표시되어 있다. 이런 2단 구조이면 반드시 "코드 (정책설명)" 형식으로 합쳐서 적는다. 예: "MWA-01 (도매 기본 정책)". 이런 구조가 아니라 제목이 하나뿐이면 그 제목을 그대로 적는다. 제목이 전혀 없으면 "정책지"라고 입력.
- group_name: 정책지 이름. policy_title과 동일한 값으로 넣는다.
- model: 모델명. 표에 적힌 글자를 그대로 입력한다. 괄호가 있으면 괄호까지 원본 그대로 적고, 절대 괄호를 풀어서 여러 행으로 나누지 않는다 (괄호 풀기는 프로그램이 자동으로 처리함). 예: SM-F971(6)NK, AIP17(P,PM)
- join_type: 가입유형. 예: MNP, 010 신규, 기변
- threshold: 해당 정책단가가 적용되는 요금제 기준 금액. 숫자만 원 단위로 입력.
- amount: 정책단가. 중요: 표 안의 숫자는 보통 뒤의 ",000"이 생략되어 적혀 있다. 즉 표에 200이라고만 적혀 있으면 실제 금액은 200,000원이라는 뜻이다. 따라서 표에서 읽은 숫자에 1000을 곱한 값을 넣어라. 예: 표에 200 → amount는 200000, 표에 150 → amount는 150000, 표에 80 → amount는 80000, 표에 50 → amount는 50000. (단, 표에 이미 "200,000"처럼 콤마와 000이 다 적혀 있는 경우는 그대로 200000으로 입력하고 다시 1000을 곱하지 않는다.)
- start_date: 이 정책지가 적용되는 기간의 시작일. 보통 표 상단 "정책 적용 일시" 영역의 "시작" 칸에 파란 글씨로 적혀 있다 (예: "9월 1일"). 적힌 글자를 그대로 적는다. 없으면 빈 문자열("").
- end_date: 정책 적용 종료일. "종료" 칸에 적힌 글자를 그대로 적는다. 만약 날짜가 아니라 "개통"처럼 날짜가 아닌 문구만 있거나 비어있으면, 종료일이 정해지지 않은 것이므로 빈 문자열("")로 적는다.

중요한 규칙:
1. 한 이미지에 모델이 여러 개 있으면 전부 추출한다.
2. 한 모델에 가입유형이 여러 개 있으면 전부 추출한다.
3. 요금제 기준이 37K 이상, 61K 이상, 70K 이상, 100K 이상, 110K 이상, 120K 이상처럼 표시되어 있으면 각각 별도 행으로 만든다.
4. "37K", "61K", "70K", "100K", "110K", "120K"는 각각 37000, 61000, 70000, 100000, 110000, 120000으로 변환한다.
5. 빈칸, -, N/A 등 정책단가가 없는 셀은 행을 만들지 않는다.
6. 표의 행/열 구조를 유지해서 모델과 가입유형, 금액이 잘못 연결되지 않도록 한다.
7. 정책단가에 콤마/원/만원 등의 표기가 있으면 원 단위 숫자로 변환한다.
8. 같은 모델/가입유형에 여러 요금제 기준이 있으면 모두 추출한다.
9. 이미지에서 읽을 수 없는 값은 추측하지 말고 가능한 경우 빈 값 대신 해당 행을 제외한다.
10. 정책지 제목은 표 제목/상단 헤더를 우선해서 읽는다.
11. 모델명에 괄호가 있으면 절대 풀지 말고 원본 그대로 적는다. 예: "SM-F971(6)NK"를 "SM-F971NK"와 "SM-F976NK"로 나누어 두 행을 만들지 말고, 반드시 "SM-F971(6)NK" 한 행으로만 적는다.
12. amount(정책단가)는 반드시 위 amount 필드 설명대로 1000을 곱해서 넣는다. 절대 표에 적힌 숫자를 그대로(예: 200을 200원으로) 넣지 않는다.
13. start_date, end_date는 한 이미지(한 정책지) 안의 모든 행에 동일하게 적용한다. 같은 이미지에서 나온 모든 모델/가입유형 행은 같은 start_date, end_date 값을 가져야 한다.

예시:
[
  {
    "policy_title": "MWA-01 (도매 기본 정책)",
    "group_name": "MWA-01 (도매 기본 정책)",
    "model": "SM-F971(6)NK",
    "join_type": "MNP",
    "threshold": 61000,
    "amount": 150000,
    "start_date": "9월 1일",
    "end_date": ""
  }
]
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    response = requests.post(
        url,
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": media_type, "data": b64_image}},
                    ]
                }
            ]
        },
        timeout=300,
    )

    if response.status_code != 200:
        try:
            err = response.json().get("error", {})
            message = err.get("message") or response.text
        except Exception:
            message = response.text

        raise RuntimeError(
            f"API 오류 ({response.status_code}) - 사용 모델: {GEMINI_MODEL}\n{message}"
        )

    data = response.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"응답을 이해할 수 없습니다: {data}")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    rows = json.loads(cleaned)

    # 기본적인 데이터 정리/검증
    cleaned_rows = []
    for row in rows:
        try:
            title = str(row.get("policy_title") or row.get("group_name") or "정책지").strip()
            model = str(row.get("model", "")).strip().upper()
            join_type = str(row.get("join_type", "")).strip()
            threshold = int(float(row.get("threshold", 0)))
            amount = int(float(row.get("amount", 0)))

            if not model or not join_type or threshold <= 0 or amount < 0:
                continue

            start_date = parse_korean_date(row.get("start_date", ""))
            end_date = parse_korean_date(row.get("end_date", ""))

            cleaned_rows.append({
                "policy_title": title,
                "group_name": title,
                "model": model,
                "join_type": join_type,
                "threshold": threshold,
                "amount": amount,
                "start_date": start_date,
                "end_date": end_date,
            })
        except (TypeError, ValueError):
            continue

    return cleaned_rows


# ----------------------------------------------------------------------
# 3. Streamlit 화면
# ----------------------------------------------------------------------

st.set_page_config(page_title="정책단가 조회", page_icon="💰", layout="centered")
init_db()

menu = st.sidebar.radio("화면 선택", ["직원 조회", "관리자"])

# ------------------------- 직원 조회 화면 -------------------------
if menu == "직원 조회":
    st.title("정책단가 조회")

    df_all = load_all()
    if df_all.empty:
        st.warning("등록된 정책 데이터가 없습니다. 관리자 화면에서 먼저 데이터를 추가해주세요.")
    else:
        models = sorted(df_all["model"].unique())

        col1, col2 = st.columns(2)
        with col1:
            model = st.selectbox("모델", models)

        join_types = sorted(
            df_all.loc[df_all["model"] == model, "join_type"].unique()
        )
        with col2:
            join_type = st.selectbox("가입유형", join_types)

        plan_options = {
            "37K 이상": 37000,
            "61K 이상": 61000,
            "100K 이상": 100000,
            "110K 이상": 110000,
            "120K 이상": 120000,
        }

        selected_plan_label = st.selectbox(
            "요금제",
            list(plan_options.keys()),
            index=1,
            help="선택한 요금제 금액 이하에서 가장 높은 정책 기준을 적용합니다."
        )

        plan_fee = plan_options[selected_plan_label]

        if st.button("정책단가 조회", type="primary", use_container_width=True):
            total, detail = calc_price(model, join_type, plan_fee)

            st.markdown("---")
            st.subheader("조회 결과")
            st.caption(f"{model}  /  {join_type}  /  {selected_plan_label}")
            st.markdown(f"## 💰 {total:,}원")

            if detail:
                st.markdown("**세부 내역**")
                st.table(pd.DataFrame(detail))
            else:
                st.info("해당 조건에 맞는 정책이 없습니다.")

# ------------------------- 관리자 화면 -------------------------
else:
    st.title("정책 관리자")

    # ---------------- 사진으로 정책 추가하기 ----------------
    with st.expander("📷 사진으로 정책 추가하기", expanded=False):
        st.caption(
            "정책단가표 사진(또는 캡처화면)을 한 장 또는 여러 장 올리면 AI가 표를 읽어서 "
            "정책지 / 모델 / 가입유형 / 적용기준 요금제 / 정책단가를 자동으로 채워줍니다."
        )

        saved_key = get_saved_api_key()
        api_key = st.text_input(
            "구글 Gemini API 키",
            value=saved_key,
            type="password",
            help="aistudio.google.com 에서 무료로 발급받은 API 키. 아래 '키 저장' 버튼을 누르면 이 폴더의 api_key.txt에 저장되어 다음부터는 다시 입력하지 않아도 됩니다.",
        )

        if st.button("🔑 키 저장"):
            if api_key:
                save_api_key(api_key)
                st.success("API 키가 저장되었습니다. 이제 아래에서 사진을 올려 분석해보세요.")
            else:
                st.warning("먼저 위 칸에 API 키를 붙여넣어 주세요.")

        uploaded_files = st.file_uploader(
            "정책표 사진 업로드",
            type=["png", "jpg", "jpeg"],
            accept_multiple_files=True,
            help="정책지 사진을 여러 장 한 번에 선택할 수 있습니다."
        )

        if uploaded_files:
            st.caption(f"선택된 파일: **{len(uploaded_files)}장**")
            for file in uploaded_files:
                st.write(f"• {file.name}")

        if st.button(
            "📷 사진 여러 장 분석하기",
            disabled=not uploaded_files or not api_key,
            use_container_width=True
        ):
            if api_key != saved_key:
                save_api_key(api_key)

            all_rows = []
            failed_files = []

            progress = st.progress(0)
            status = st.empty()

            with st.spinner("여러 정책지 사진을 순서대로 분석하는 중..."):
                for i, uploaded in enumerate(uploaded_files, start=1):
                    status.write(f"({i}/{len(uploaded_files)}) **{uploaded.name}** 분석 중...")

                    try:
                        media_type = (
                            "image/png"
                            if uploaded.name.lower().endswith(".png")
                            else "image/jpeg"
                        )

                        rows = extract_policy_rows_from_image(
                            uploaded.getvalue(),
                            media_type,
                            api_key
                        )

                        # 모델명에 괄호(예: SM-F971(6)NK, AIP17(P,PM))가 있으면
                        # 실제 모델 여러 개로 풀어서 각각 별도 행으로 만든다.
                        expanded_rows = []
                        for row in rows:
                            for model_variant in expand_model_variants(row.get("model", "")):
                                new_row = dict(row)
                                new_row["model"] = model_variant
                                expanded_rows.append(new_row)

                        # 각 이미지에서 인식된 행을 모두 하나로 합침
                        all_rows.extend(expanded_rows)

                    except Exception as e:
                        failed_files.append(f"{uploaded.name}: {e}")

                    progress.progress(i / len(uploaded_files))

            status.empty()

            if all_rows:
                st.session_state["ocr_rows"] = all_rows
                st.success(
                    f"총 {len(uploaded_files)}장 중 "
                    f"{len(uploaded_files) - len(failed_files)}장 분석 완료 · "
                    f"인식 데이터 {len(all_rows)}건"
                )

            if failed_files:
                st.warning("일부 사진은 분석하지 못했습니다.")
                for msg in failed_files:
                    st.write(f"⚠️ {msg}")

            if not all_rows:
                st.session_state.pop("ocr_rows", None)
                st.error("분석된 정책 데이터가 없습니다.")

        if "ocr_rows" in st.session_state and st.session_state["ocr_rows"]:
            st.markdown("**AI 인식 결과 — 저장 전에 반드시 확인하세요.**")
            preview_df = pd.DataFrame(st.session_state["ocr_rows"])

            if "policy_title" not in preview_df.columns:
                preview_df["policy_title"] = preview_df["group_name"]
            if "start_date" not in preview_df.columns:
                preview_df["start_date"] = None
            if "end_date" not in preview_df.columns:
                preview_df["end_date"] = None

            # 같은 정책지(영어-숫자 코드, 예: MWA-01)끼리 묶어서 표를 나눈다
            preview_df["_code"] = preview_df["policy_title"].apply(extract_policy_code)
            codes = sorted(preview_df["_code"].dropna().astype(str).unique().tolist())

            st.info(
                f"인식된 정책지 **{len(codes)}개** · "
                f"총 **{len(preview_df)}건**"
            )

            edited_groups = {}
            for code in codes:
                gdf = preview_df[preview_df["_code"] == code].reset_index(drop=True)
                # 표 제목(정책 설명)과 기간은 같은 정책지의 첫 행 기준으로 대표 표시
                display_title = gdf["policy_title"].iloc[0]
                period_str = format_period(gdf["start_date"].iloc[0], gdf["end_date"].iloc[0])

                st.markdown(f"#### 🗂️ {display_title}")
                st.caption(f"정책 기간: {period_str}")

                edited_groups[code] = st.data_editor(
                    gdf[["policy_title", "model", "start_date", "end_date", "join_type", "threshold", "amount"]],
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"ocr_preview_editor_{code}",
                    column_config={
                        "policy_title": st.column_config.TextColumn("정책지 제목"),
                        "model": st.column_config.TextColumn("모델"),
                        "join_type": st.column_config.TextColumn("가입유형"),
                        "threshold": st.column_config.NumberColumn(
                            "적용 기준 요금제(원, 이상)", step=1000
                        ),
                        "amount": st.column_config.NumberColumn(
                            "정책단가(원)", step=1000
                        ),
                        "start_date": st.column_config.TextColumn(
                            "시작일(YYYY-MM-DD)", disabled=True
                        ),
                        "end_date": st.column_config.TextColumn(
                            "종료일(YYYY-MM-DD, 비어있으면 진행중)", disabled=True
                        ),
                    },
                )
                st.markdown("---")

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("✅ 이 내용을 정책표에 추가", type="primary", use_container_width=True):
                    all_edited = pd.concat(edited_groups.values(), ignore_index=True)
                    rows_to_add = all_edited.dropna(
                        subset=["policy_title", "model", "join_type", "threshold", "amount"]
                    ).to_dict("records")

                    for row in rows_to_add:
                        row["group_name"] = row.pop("policy_title")

                    insert_rows(rows_to_add)
                    st.session_state.pop("ocr_rows", None)
                    st.success(f"{len(rows_to_add)}건이 추가되었습니다.")
                    st.rerun()
            with col_b:
                if st.button("취소", use_container_width=True):
                    st.session_state.pop("ocr_rows", None)
                    st.rerun()

    st.markdown("---")
    st.caption("정책지별로 표가 나뉘어 있습니다. 각 표를 직접 수정 / 추가 / 삭제한 뒤 '저장' 버튼을 눌러주세요.")

    df = load_all()

    if df.empty:
        st.info("등록된 정책 데이터가 없습니다.")
        edited = df
    else:
        # id 컬럼은 화면에서 숨기고 편집만 나머지 컬럼으로
        edit_df = df.drop(columns=["id"]) if "id" in df.columns else df
        edit_df = edit_df.reset_index(drop=True)
        # 컬럼 순서: 정책지 / 모델 / 기간(시작~종료) / 가입유형 / 요금제기준 / 정책단가
        col_order = ["group_name", "model", "start_date", "end_date", "join_type", "threshold", "amount"]
        edit_df = edit_df[[c for c in col_order if c in edit_df.columns]]
        edit_df["_code"] = edit_df["group_name"].apply(extract_policy_code)
        codes = sorted(edit_df["_code"].dropna().astype(str).unique().tolist())

        admin_col_config = {
            "group_name": st.column_config.TextColumn("정책지", help="예: MWA-01 (도매 기본 정책)"),
            "model": st.column_config.TextColumn("모델", help="예: F971"),
            "join_type": st.column_config.TextColumn("가입유형", help="예: MNP, 기변, 신규"),
            "threshold": st.column_config.NumberColumn("적용 기준 요금제(원, 이상)", step=1000),
            "amount": st.column_config.NumberColumn("정책단가(원)", step=1000),
            "start_date": st.column_config.TextColumn(
                "시작일(YYYY-MM-DD)", help="비워두면 시작일 제한 없음"
            ),
            "end_date": st.column_config.TextColumn(
                "종료일(YYYY-MM-DD)", help="비워두면 종료일 미정(계속 유효)"
            ),
        }

        edited_frames = []
        for code in codes:
            gdf = edit_df[edit_df["_code"] == code].drop(columns=["_code"]).reset_index(drop=True)
            display_title = gdf["group_name"].iloc[0]
            period_str = format_period(gdf["start_date"].iloc[0], gdf["end_date"].iloc[0])

            st.markdown(f"#### 🗂️ {display_title}")
            st.caption(f"정책 기간: {period_str}")

            edited_g = st.data_editor(
                gdf,
                num_rows="dynamic",
                use_container_width=True,
                key=f"admin_editor_{code}",
                column_config=admin_col_config,
            )
            edited_frames.append(edited_g)
            st.markdown("---")

        edited = pd.concat(edited_frames, ignore_index=True) if edited_frames else edit_df.drop(columns=["_code"])

    if st.button("저장", type="primary"):
        save_all(edited)
        st.success("저장되었습니다.")
        st.rerun()

    with st.expander("➕ 완전히 새로운 정책지 추가"):
        st.caption("위 표들에 없는 새 정책지를 처음부터 추가할 때 사용하세요. (기존 정책지에 항목만 추가하려면 위 표에서 직접 행을 추가하면 됩니다.)")
        with st.form("new_group_form", clear_on_submit=True):
            ng_col1, ng_col2 = st.columns(2)
            with ng_col1:
                new_group_name = st.text_input("정책지 이름", placeholder="예: MWA-02 (도매 추가 정책)")
                new_model = st.text_input("모델", placeholder="예: SM-F971NK")
                new_join_type = st.text_input("가입유형", placeholder="예: MNP")
            with ng_col2:
                new_threshold = st.number_input("적용 기준 요금제(원, 이상)", min_value=0, step=1000)
                new_amount = st.number_input("정책단가(원)", min_value=0, step=1000)
                new_start = st.text_input("시작일(YYYY-MM-DD, 비워두면 제한없음)")
                new_end = st.text_input("종료일(YYYY-MM-DD, 비워두면 진행중)")

            if st.form_submit_button("추가"):
                if new_group_name and new_model and new_join_type:
                    insert_rows([{
                        "group_name": new_group_name,
                        "model": new_model,
                        "join_type": new_join_type,
                        "threshold": int(new_threshold),
                        "amount": int(new_amount),
                        "start_date": new_start or None,
                        "end_date": new_end or None,
                    }])
                    st.success("새 정책지가 추가되었습니다.")
                    st.rerun()
                else:
                    st.warning("정책지 이름 / 모델 / 가입유형은 필수입니다.")

    st.markdown("---")
    st.caption("엑셀로 관리하던 정책표가 있다면, 엑셀에서 복사(Ctrl+C)해서 위 표에 붙여넣기(Ctrl+V)도 가능합니다.")
