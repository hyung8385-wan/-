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
GEMINI_MODEL = "gemini-3.6-flash"  # 정책지 이미지 분석용 최신 Flash 모델

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
    """정책지 이미지에서 제목/모델/가입유형/요금제 기준/정책단가를 추출한다."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """
이 이미지는 휴대폰 판매 정책지(정책단가표)이다.
이미지 전체를 꼼꼼하게 읽고, 표에 있는 모든 모델/가입유형/요금제 기준/정책단가를 추출해줘.

반드시 JSON 배열만 출력하고 설명은 출력하지 마.

각 행은 아래 6개 필드를 가져야 한다.
- policy_title: 이미지 상단의 정책지 제목/문서명/정책명. 제목이 명확하면 그대로 적고, 날짜가 있으면 날짜도 포함. 제목이 전혀 없으면 "정책지"라고 입력.
- group_name: 정책지 이름. policy_title과 동일한 값으로 넣는다.
- model: 모델명. 예: F971, F976, S931
- join_type: 가입유형. 예: MNP, 010 신규, 기변
- threshold: 해당 정책단가가 적용되는 요금제 기준 금액. 숫자만 원 단위로 입력.
- amount: 정책단가. 숫자만 원 단위로 입력.

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

예시:
[
  {
    "policy_title": "9월 15일 갤럭시 정책",
    "group_name": "9월 15일 갤럭시 정책",
    "model": "F971",
    "join_type": "MNP",
    "threshold": 61000,
    "amount": 100000
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
        timeout=180,
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

            cleaned_rows.append({
                "policy_title": title,
                "group_name": title,
                "model": model,
                "join_type": join_type,
                "threshold": threshold,
                "amount": amount,
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
            "70K 이상": 70000,
            "100K 이상": 100000,
            "110K 이상": 110000,
            "120K 이상": 120000,
        }

        selected_plan_label = st.selectbox(
            "요금제",
            list(plan_options.keys()),
            index=2,
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

                        # 각 이미지에서 인식된 행을 모두 하나로 합침
                        all_rows.extend(rows)

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

            # 여러 정책지가 섞여 있어도 제목별로 확인 가능
            policy_titles = preview_df["policy_title"].dropna().astype(str).unique().tolist()

            st.info(
                f"인식된 정책지 **{len(policy_titles)}개** · "
                f"총 **{len(preview_df)}건**"
            )

            if len(policy_titles) > 1:
                st.caption(
                    "여러 정책지의 결과가 한 번에 표시됩니다. "
                    "정책지 제목을 기준으로 각 행을 확인한 뒤 저장하세요."
                )

            edited_preview = st.data_editor(
                preview_df[[
                    "policy_title", "model", "join_type", "threshold", "amount"
                ]],
                num_rows="dynamic",
                use_container_width=True,
                key="ocr_preview_editor",
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
                },
            )

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("✅ 이 내용을 정책표에 추가", type="primary", use_container_width=True):
                    rows_to_add = edited_preview.dropna(
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
