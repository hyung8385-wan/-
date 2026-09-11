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
import json
import os
import sqlite3

import pandas as pd
import requests
import streamlit as st

DB_PATH = "policy.db"
API_KEY_PATH = "api_key.txt"  # 구글 Gemini API 키를 저장해두는 파일 (같은 폴더에 생성됨)
GEMINI_MODEL = "gemini-2.5-flash"  # 무료 등급으로 사용 가능한 모델

# ----------------------------------------------------------------------
# 1. DB 관련 함수
# ----------------------------------------------------------------------

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    """policies 테이블이 없으면 만들고, 비어있으면 샘플 데이터를 넣는다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,   -- 정책지 A / B / C ...
            model TEXT NOT NULL,        -- F971 등
            join_type TEXT NOT NULL,    -- MNP / 기변 / 신규 등
            threshold INTEGER NOT NULL, -- 이 금액 "이상"부터 적용되는 요금제 기준(원)
            amount INTEGER NOT NULL     -- 지급되는 정책단가(원)
        )
        """
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM policies")
    count = cur.fetchone()[0]
    if count == 0:
        sample = [
            ("정책지 A", "F971", "MNP", 37000, 50000),
            ("정책지 A", "F971", "MNP", 61000, 100000),
            ("정책지 A", "F971", "MNP", 100000, 150000),
            ("정책지 B", "F971", "MNP", 61000, 50000),
            ("정책지 C", "F971", "MNP", 61000, 40000),
        ]
        cur.executemany(
            "INSERT INTO policies (group_name, model, join_type, threshold, amount) VALUES (?,?,?,?,?)",
            sample,
        )
        conn.commit()
    conn.close()


def load_all() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM policies ORDER BY group_name, threshold", conn)
    conn.close()
    return df


def save_all(df: pd.DataFrame):
    """관리자 화면에서 편집한 표 전체를 DB에 덮어쓴다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM policies")
    for _, row in df.iterrows():
        # 빈 행(신규 추가하다가 값을 안 채운 행)은 건너뜀
        if pd.isna(row.get("group_name")) or pd.isna(row.get("model")):
            continue
        cur.execute(
            "INSERT INTO policies (group_name, model, join_type, threshold, amount) VALUES (?,?,?,?,?)",
            (
                str(row["group_name"]),
                str(row["model"]),
                str(row["join_type"]),
                int(row["threshold"]),
                int(row["amount"]),
            ),
        )
    conn.commit()
    conn.close()


def insert_rows(rows: list[dict]):
    """사진에서 추출한 정책 행들을 기존 데이터에 추가(append)한다. 덮어쓰지 않음."""
    conn = get_conn()
    cur = conn.cursor()
    for row in rows:
        cur.execute(
            "INSERT INTO policies (group_name, model, join_type, threshold, amount) VALUES (?,?,?,?,?)",
            (
                str(row["group_name"]),
                str(row["model"]),
                str(row["join_type"]),
                int(row["threshold"]),
                int(row["amount"]),
            ),
        )
    conn.commit()
    conn.close()


def calc_price(model: str, join_type: str, plan_fee: int):
    """선택한 조건에 맞는 정책지별 최고 구간 금액을 합산한다."""
    df = load_all()
    df = df[(df["model"] == model) & (df["join_type"] == join_type)]

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
    """사진(정책표) 이미지를 구글 Gemini에게 보내서 정책 데이터 목록(JSON)을 추출해온다."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    prompt = (
        "이 사진은 통신사 정책단가표야. 사진 속 표(또는 내용)를 읽어서 "
        "아래 형식의 JSON 배열로만 답해줘. 다른 설명이나 코드블럭(```) 없이 JSON 배열만 출력해.\n\n"
        "각 항목의 필드:\n"
        "- group_name: 정책지 이름 (예: '정책지 A'). 표에 구분이 없으면 '정책지 A'로 통일\n"
        "- model: 모델명 (예: 'F971')\n"
        "- join_type: 가입유형 (예: 'MNP', '기변', '신규')\n"
        "- threshold: 이 금액 이상부터 적용되는 요금제 기준, 숫자만(원 단위, 콤마/원 제외)\n"
        "- amount: 정책단가 금액, 숫자만(원 단위, 콤마/원 제외)\n\n"
        "예시 출력:\n"
        '[{"group_name":"정책지 A","model":"F971","join_type":"MNP","threshold":61000,"amount":100000}]'
    )

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
        timeout=60,
    )

    if response.status_code != 200:
        raise RuntimeError(f"API 오류 ({response.status_code}): {response.text}")

    data = response.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"응답을 이해할 수 없습니다: {data}")

    # 혹시 모델이 코드블럭(```json ... ```)으로 감싸서 답했으면 벗겨낸다
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip() if cleaned.lower().startswith("json") else cleaned

    rows = json.loads(cleaned)
    return rows


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
        join_types = sorted(df_all["join_type"].unique())

        col1, col2 = st.columns(2)
        with col1:
            model = st.selectbox("모델", models)
        with col2:
            join_type = st.selectbox("가입유형", join_types)

        plan_fee = st.number_input(
            "요금제 (원)", min_value=0, step=1000, value=70000,
            help="예: 70,000원짜리 요금제라면 70000 입력"
        )

        if st.button("정책단가 조회", type="primary", use_container_width=True):
            total, detail = calc_price(model, join_type, plan_fee)

            st.markdown("---")
            st.subheader("최종 정책단가")
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
            "정책단가표 사진(또는 캡처화면)을 올리면 AI가 표를 읽어서 "
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

        uploaded = st.file_uploader("정책표 사진 업로드", type=["png", "jpg", "jpeg"])

        if st.button("사진 분석하기", disabled=uploaded is None or not api_key):
            if api_key != saved_key:
                save_api_key(api_key)

            media_type = "image/png" if uploaded.name.lower().endswith("png") else "image/jpeg"
            with st.spinner("사진 속 정책표를 읽는 중..."):
                try:
                    rows = extract_policy_rows_from_image(uploaded.getvalue(), media_type, api_key)
                    st.session_state["ocr_rows"] = rows
                except Exception as e:
                    st.error(f"분석에 실패했습니다: {e}")
                    st.session_state.pop("ocr_rows", None)

        if "ocr_rows" in st.session_state and st.session_state["ocr_rows"]:
            st.markdown("**인식된 내용 (수정 후 추가하세요)**")
            preview_df = pd.DataFrame(st.session_state["ocr_rows"])
            edited_preview = st.data_editor(
                preview_df,
                num_rows="dynamic",
                use_container_width=True,
                key="ocr_preview_editor",
                column_config={
                    "group_name": st.column_config.TextColumn("정책지"),
                    "model": st.column_config.TextColumn("모델"),
                    "join_type": st.column_config.TextColumn("가입유형"),
                    "threshold": st.column_config.NumberColumn("적용 기준 요금제(원, 이상)", step=1000),
                    "amount": st.column_config.NumberColumn("정책단가(원)", step=1000),
                },
            )

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("✅ 이 내용을 정책표에 추가", type="primary", use_container_width=True):
                    rows_to_add = edited_preview.dropna(subset=["group_name", "model"]).to_dict("records")
                    insert_rows(rows_to_add)
                    st.session_state.pop("ocr_rows", None)
                    st.success(f"{len(rows_to_add)}건이 추가되었습니다.")
                    st.rerun()
            with col_b:
                if st.button("취소", use_container_width=True):
                    st.session_state.pop("ocr_rows", None)
                    st.rerun()

    st.markdown("---")
    st.caption("표를 직접 수정 / 추가 / 삭제한 뒤 '저장' 버튼을 눌러주세요.")

    df = load_all()
    # id 컬럼은 화면에서 숨기고 편집만 나머지 컬럼으로
    edit_df = df.drop(columns=["id"]) if "id" in df.columns else df

    edited = st.data_editor(
        edit_df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "group_name": st.column_config.TextColumn("정책지", help="예: 정책지 A"),
            "model": st.column_config.TextColumn("모델", help="예: F971"),
            "join_type": st.column_config.TextColumn("가입유형", help="예: MNP, 기변, 신규"),
            "threshold": st.column_config.NumberColumn("적용 기준 요금제(원, 이상)", step=1000),
            "amount": st.column_config.NumberColumn("정책단가(원)", step=1000),
        },
    )

    if st.button("저장", type="primary"):
        save_all(edited)
        st.success("저장되었습니다.")
        st.rerun()

    st.markdown("---")
    st.caption("엑셀로 관리하던 정책표가 있다면, 엑셀에서 복사(Ctrl+C)해서 위 표에 붙여넣기(Ctrl+V)도 가능합니다.")
